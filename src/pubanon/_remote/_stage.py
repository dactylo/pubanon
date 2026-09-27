"""Push retained candidates to a persistent private staging repository."""

# pyright: reportPrivateUsage=false

import os
import re
import tempfile
import time
import tomllib
from contextlib import suppress
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
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _host
from pubanon._remote._host import (
    CheckVerdict,
    Destination,
    RemoteRefs,
    SettingsFacts,
    check_recovery,
    check_result,
    create_repository,
    delete_repository,
    push,
    read_remote_refs,
    repository_facts,
    repository_settings,
    repository_visible,
    require_authenticated_account,
    set_default_branch,
)
from pubanon._run import OID_HEX
from pubanon._snapshots._candidate import (
    candidate_store_path,
    inspect_next_action,
    load_candidate,
    publication_archive,
    store_path,
    verify_retained_candidate,
)
from pubanon._snapshots._records import CandidateRecord, candidate_tip, load_publication
from pubanon._terminal import (
    confirm,
    detail,
    detail_fields,
    display_check_result,
    escape,
    field,
    fields,
    heading,
    next_step,
    output,
)
from pubanon._toml import toml_string

_OID_PATTERN = re.compile(OID_HEX)
_STAGE_RECORD_KEYS = frozenset({'commit', 'owner', 'repo'})
_STAGE_REF = 'refs/heads/main'
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


@dataclass(frozen=True)
class _StageObservation:
    """Keep refs and default branch from one private destination observation."""

    refs: RemoteRefs
    default: str


class _InvalidStageRecordError(PubanonError):
    """Identify malformed persisted staging state for cleanup routing."""


def _backup_stage(path: Path, content: bytes) -> None:
    """Keep exact malformed bytes in a unique private sibling before replacement."""
    descriptor: int | None = None
    backup: Path | None = None
    complete = False
    try:
        descriptor, name = tempfile.mkstemp(prefix=f'{path.name}.backup-', dir=path.parent)
        backup = Path(name)
        _filesystem.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _filesystem.sync_directory(path.parent)
        complete = True
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot back up malformed stage record') from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if backup is not None and not complete:
            with suppress(OSError):
                backup.unlink(missing_ok=True)


def _preview_stage_push(
    dest: Destination,
    record: CandidateRecord,
    stage: StageRecord | None,
    refs: RemoteRefs,
    settings: SettingsFacts,
) -> None:
    """Show the exact configured replacement and prior advisory tracking."""
    tip = candidate_tip(record)
    expected_old = refs.main
    fields(
        [
            ('Staging repository', f'{escape(dest.owner)}/{escape(dest.repo)} (private)'),
            ('Branch', 'main'),
            ('Current snapshot', expected_old[:12] if expected_old else 'none'),
            ('Prepared snapshot', f'{escape(record.tag_intent or "untagged")} ({tip[:12]})'),
        ],
        indent=2,
    )
    detail_fields(
        [
            ('Recorded destination', escape(f'{stage._owner}/{stage._repo}' if stage else 'none')),
            ('Recorded staged commit', escape(stage.commit if stage else 'none')),
            ('Observed remote commit', escape(expected_old or 'absent')),
            ('Candidate tip', escape(tip)),
        ],
        indent=2,
    )
    if expected_old is None and settings.default_branch != 'main':
        output(
            '  '
            + field(
                'Default branch change', f'{escape(settings.default_branch)} -> main after push'
            )
        )
    detail('  Git details:')
    detail(f'  {escape(tip)}:{_STAGE_REF}')
    detail(f'  force-with-lease {_STAGE_REF}:{escape(expected_old or "absent")}')


def _preview_stage_repair(
    dest: Destination,
    stage: StageRecord | None,
    observed: _StageObservation,
    record: StageRecord,
) -> None:
    """Preview every intended default-branch and local-record change."""
    heading('Staging repair preview')
    output(field('Repository', escape(f'{dest.owner}/{dest.repo}')))
    output(field('Repository URL', escape(dest.url)))
    output(field('Visibility', 'private'))
    output(
        field('Recorded destination', escape(f'{stage._owner}/{stage._repo}' if stage else 'none'))
    )
    output(field('Recorded staged commit', escape(stage.commit if stage else 'none')))
    output(field('Observed remote commit', escape(record.commit)))
    if observed.default != 'main':
        output(field('Default branch change', f'{escape(observed.default)} -> main'))
    if record != stage:
        output('Save local tracking for this recognized snapshot.')
    else:
        output('Local tracking is current and will remain unchanged.')
    if stage is not None and (stage._owner, stage._repo) != (dest.owner, dest.repo):
        output('The previously recorded repository will remain unchanged.')


def _push_stage_main(dest: Destination, store: Path, tip: str, observed_tip: str | None) -> None:
    """Push only main with a lease against the freshly observed tip."""
    try:
        push(
            dest,
            store,
            [
                '--atomic',
                f'--force-with-lease={_STAGE_REF}:{observed_tip or ""}',
                '--no-follow-tags',
                f'{tip}:{_STAGE_REF}',
            ],
        )
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise PubanonError(
            'staging push failed; remote outcome must be inspected before retrying',
            fact=exc.render(),
            recovery=(
                'inspect the staging repository; run pubanon stage repair '
                'or retry pubanon stage push'
            ),
        ) from exc


def _record_string(data: dict[str, object], key: str) -> str:
    """Return one required stage-record string."""
    value = data.get(key)
    if not isinstance(value, str):
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='pubanon stage repair or pubanon stage forget',
        )
    return value


def _remaining_timeout(timeout: float, started: float) -> float:
    """Return the positive remainder of one supplied timeout budget."""
    remaining = timeout - (_monotonic() - started)
    if remaining <= 0:
        raise ObservationTimeoutError
    return remaining


def _report_stage_default_change(refs: RemoteRefs, settings: SettingsFacts, *, yes: bool) -> None:
    """Show the default-branch change when a push skips its confirmation preview."""
    if refs.main is None and settings.default_branch != 'main' and yes:
        output(
            field('Default branch change', f'{escape(settings.default_branch)} -> main after push')
        )


def _require_repair_tip(expected: str, actual: str | None) -> None:
    """Refuse a concurrent branch change before recording repair success."""
    if actual != expected:
        raise PubanonError(
            'staging tip changed during repair', expected=expected, actual=actual or 'absent'
        )


def _require_stage_facts(
    dest: Destination, actual: tuple[str, str, str], *, recovery: str
) -> None:
    """Require one observed repository to match private staging coordinates."""
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
        url=_host.repository_url(p.owner, name),
        account=g.accounts[p.host],
        visibility='private',
    )


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


def _status_checks(dest: Destination, stage: StageRecord, p: ProjectConfig) -> None:
    """Report ordered CI verdicts for one identified exact staging commit."""
    if not p.required_checks:
        output('No required checks are configured for the staged snapshot.', kind='warning')
        return

    verdicts: list[CheckVerdict] = []
    for name in p.required_checks:
        verdict = check_result((dest.owner, dest.repo), stage.commit, name)
        verdicts.append(verdict)
    if all(verdict.state == 'success' for verdict in verdicts):
        output(
            field(
                'Checks',
                ', '.join(
                    f'{escape(verdict.name)} {display_check_result(verdict)}'
                    for verdict in verdicts
                ),
            )
        )
        return
    for verdict in verdicts:
        output(
            '  '
            + field(
                escape(verdict.name),
                display_check_result(verdict),
                kind='warning',
            )
        )
    raise PubanonError(
        'required checks are not acceptable for the staged commit',
        fact=','.join(f'{escape(verdict.name)}:{escape(verdict.state)}' for verdict in verdicts),
        recovery=check_recovery(verdicts, retry='rerun `pubanon stage status`'),
    )


def _status_commit(commit: str | None, label: str, shown_commits: dict[str, str]) -> str:
    """Render a full commit once and name an earlier field on repetition."""
    if commit is None:
        return 'absent'
    previous = shown_commits.get(commit)
    if previous is not None:
        return f'matches {previous}'
    shown_commits[commit] = label
    return escape(commit)


def _status_candidate(
    paths: Paths, p: ProjectConfig, commit: str | None, shown_commits: dict[str, str]
) -> None:
    """Report how the prepared candidate relates to this exact staged snapshot."""
    candidate = load_candidate(paths, p.name)
    if candidate is None:
        output(field('Prepared', 'none'))
        return
    tip = candidate_tip(candidate)
    rendered = (
        'matches staged commit'
        if commit is not None and tip == commit
        else _status_commit(tip, 'prepared candidate', shown_commits)
    )
    detail(field('Prepared candidate', rendered))
    if commit is None:
        detail(field('Prepared candidate match', 'unknown'))
        return
    matches = tip == commit
    if not matches:
        output(field('Prepared', escape(tip[:12])))
        output('Staged check results do not cover the prepared candidate.', kind='warning')
        output('Run pubanon stage push to check the prepared candidate in staging.')


def _unrecognized_stage_tip(dest: Destination, tip: str | None) -> PubanonError:
    """Explain an unresolved snapshot without adopting remote history."""
    return PubanonError(
        'remote staging snapshot is not identified by local evidence'
        if tip is not None
        else 'staging repository has no staged snapshot',
        fact=(
            f'https://github.com/{dest.owner}/{dest.repo}/commit/{tip}'
            if tip is not None
            else dest.url
        ),
        recovery=(
            'inspect the staging repository, then use pubanon stage push to '
            'stage the reviewed prepared snapshot'
        ),
    )


def _validate_stage(record: StageRecord) -> None:
    """Validate one in-memory stage record against the frozen schema."""
    try:
        validate_host_segment('owner', record._owner)
        validate_host_segment('repo', record._repo)
    except PubanonError as exc:
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='pubanon stage repair or pubanon stage forget',
        ) from exc
    if _OID_PATTERN.fullmatch(record.commit) is None:
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='pubanon stage repair or pubanon stage forget',
        )


def _parse_stage(content: bytes) -> StageRecord:
    """Decode and validate exact persisted staging bytes."""
    try:
        data = tomllib.loads(content.decode('utf-8'))
    except (UnicodeError, ValueError) as exc:
        raise _InvalidStageRecordError(
            'invalid stage record', recovery='pubanon stage repair or pubanon stage forget'
        ) from exc
    if data.keys() != _STAGE_RECORD_KEYS:
        raise _InvalidStageRecordError(
            'invalid stage record', recovery='pubanon stage repair or pubanon stage forget'
        )
    record = StageRecord(
        commit=_record_string(data, 'commit'),
        _owner=_record_string(data, 'owner'),
        _repo=_record_string(data, 'repo'),
    )
    _validate_stage(record)
    return record


def _save_stage(paths: Paths, project: str, record: StageRecord) -> None:
    """Atomically save a strict private record, backing up malformed old bytes."""
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
            recovery='pubanon stage repair or pubanon stage forget',
        ) from exc
    path = _stage_path(paths, project)
    create_data_dirs(paths, project)
    try:
        previous = path.read_bytes()
    except FileNotFoundError:
        previous = None
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot read existing stage record') from exc
    if previous is not None:
        try:
            _parse_stage(previous)
        except _InvalidStageRecordError:
            _backup_stage(path, previous)
    descriptor: int | None = None
    temporary: Path | None = None
    replaced = False
    try:
        descriptor, name = tempfile.mkstemp(prefix=f'{path.name}.tmp-', dir=path.parent)
        temporary = Path(name)
        _filesystem.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as handle:
            descriptor = None
            handle.write(content.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        _filesystem.replace(temporary, path)
        replaced = True
        _filesystem.sync_directory(path.parent)
    except (OSError, RuntimeError, UnicodeError) as exc:
        message = (
            'cannot confirm durable stage record save' if replaced else 'cannot save stage record'
        )
        raise PubanonError(message) from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _record_stage_push(paths: Paths, project: str, record: StageRecord) -> None:
    """Record a verified remote update without claiming a failed save rolled back."""
    try:
        _save_stage(paths, project, record)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise PubanonError(
            'cannot confirm staging push recording; remote update succeeded',
            fact=exc.render(),
            recovery='pubanon stage repair',
        ) from exc


def _save_repaired_stage(paths: Paths, project: str, record: StageRecord) -> None:
    """Keep local-save failure distinct from the observed remote state."""
    try:
        _save_stage(paths, project, record)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise PubanonError(
            'cannot save repaired staging tracking',
            fact=exc.render(),
            recovery='pubanon stage repair',
        ) from exc


def _verified_stage_tip(
    paths: Paths, g: GlobalConfig, p: ProjectConfig, record: CandidateRecord
) -> str:
    """Verify retained candidate gates and select the resulting commit."""
    verify_retained_candidate(paths, g, p, record, env=os.environ)
    return candidate_tip(record)


def _recognized_stage_record(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    observed: StageRecord,
    stage: StageRecord | None,
) -> StageRecord | None:
    """Identify the observed tip using independent existing local evidence."""
    tip = observed.commit
    if stage == observed:
        return stage
    if load_publication(paths, p.name, tip) is not None and publication_archive(
        paths, p.name, tip
    ):
        return observed
    candidate = load_candidate(paths, p.name)
    if candidate is not None and candidate_tip(candidate) == tip:
        _verified_stage_tip(paths, g, p, candidate)
        return observed
    return None


def _verify_stage_default(default_branch: str) -> None:
    """Require main as the default for an existing staged branch."""
    if default_branch != 'main':
        raise PubanonError(
            'staging repository default branch is not main',
            actual=escape(default_branch),
            recovery='pubanon stage repair',
        )


def _verify_stage_facts(
    dest: Destination,
    *,
    recovery: str,
    timeout: float | None = None,
) -> None:
    """Require exact private facts for one staging destination."""
    actual = repository_facts(dest.owner, dest.repo, timeout=timeout, missing_recovery=recovery)
    _require_stage_facts(dest, actual, recovery=recovery)


def _verify_stage_refs(refs: RemoteRefs) -> None:
    """Refuse legacy staging and references outside the managed main branch."""
    if (
        refs.main is None
        and not refs.tags
        and tuple(name for name, _ in refs.branches) == ('stage',)
    ):
        raise PubanonError(
            'staging repository uses the old stage branch',
            recovery=(
                'rename stage to main through GitHub and set main as the default branch, '
                'then run pubanon stage repair'
            ),
        )
    unexpected = [f'refs/heads/{name}' for name, _ in refs.branches]
    unexpected.extend(f'refs/tags/{name}' for name in refs.tags)
    if unexpected:
        raise PubanonError(
            'staging repository contains an unexpected staging reference',
            fact=', '.join(escape(ref) for ref in unexpected),
            recovery='correct the unexpected refs deliberately with Git or GitHub, then retry',
        )


def _finish_stage_push(
    dest: Destination, tip: str, refs: RemoteRefs, settings: SettingsFacts
) -> None:
    """Verify the pushed refs and select the first main as default when needed."""
    readback = read_remote_refs(dest)
    _verify_stage_refs(readback)
    if readback.main != tip:
        raise PubanonError(
            'staging read-back does not match the prepared snapshot commit',
            expected=escape(tip),
            actual=escape(readback.main or 'absent'),
        )

    if refs.main is None and settings.default_branch != 'main':
        set_default_branch(dest.owner, dest.repo, 'main')
    _verify_stage_facts(dest, recovery='pubanon stage repair')
    _verify_stage_default(repository_settings(dest.owner, dest.repo).default_branch)


def _observe_stage_repair(dest: Destination) -> _StageObservation:
    """Read current private facts and supported staging refs without mutation."""
    _verify_stage_facts(dest, recovery='inspect GitHub access, then rerun pubanon stage repair')
    refs = read_remote_refs(dest)
    _verify_stage_refs(refs)
    settings = repository_settings(dest.owner, dest.repo)
    return _StageObservation(refs, settings.default_branch)


def _repair_stage_default(dest: Destination, tip: str) -> None:
    """Request main as default once, then verify unchanged private staging state."""
    recovery = (
        'inspect GitHub permissions and the staging repository; '
        'verify main and its default branch, then rerun pubanon stage repair'
    )
    try:
        set_default_branch(dest.owner, dest.repo, 'main')
        observed = _observe_stage_repair(dest)
        _require_repair_tip(tip, observed.refs.main)
        _verify_stage_default(observed.default)
    except OperationCanceled as exc:
        exc.recovery = recovery
        raise
    except PubanonError as exc:
        raise PubanonError(
            'cannot verify staging default repair; prior tracking was retained',
            fact=exc.render(),
            recovery=recovery,
        ) from exc


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
        content = path.read_bytes()
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot read stage record') from exc
    return _parse_stage(content)


def _repair_stage(paths: Paths, g: GlobalConfig, p: ProjectConfig, *, yes: bool) -> None:
    """Preview and reconnect supported staging state from current observations."""
    dest = _stage_destination(paths, g, p)
    try:
        stage = load_stage(paths, p.name)
    except _InvalidStageRecordError:
        stage = None
        output('Malformed tracking will be backed up before replacement.', kind='warning')
    require_authenticated_account(dest.account, authority='staging configuration')
    observed = _observe_stage_repair(dest)
    tip = observed.refs.main
    if tip is None:
        raise _unrecognized_stage_tip(dest, None)
    record = _recognized_stage_record(paths, g, p, StageRecord(tip, dest.owner, dest.repo), stage)
    if record is None:
        raise _unrecognized_stage_tip(dest, tip)
    if record == stage and observed.default == 'main':
        output('Staging tracking is current; no changes are needed.', kind='success')
        return
    _preview_stage_repair(dest, stage, observed, record)
    if not yes and not confirm('Apply the staging repair?'):
        raise OperationCanceled('Canceled. Nothing was changed.')
    if observed.default != 'main':
        _repair_stage_default(dest, tip)
        output('Staging main and its default branch are verified.', kind='success')
    if record != stage:
        _save_repaired_stage(paths, p.name, record)
    output('Staging tracking repaired for the recognized snapshot.', kind='success')


def _status_tracking(
    paths: Paths, p: ProjectConfig, shown_commits: dict[str, str]
) -> tuple[StageRecord | None, PubanonError | None]:
    """Print available tracking without concealing malformed or unreadable state."""
    try:
        stage = load_stage(paths, p.name)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        output(field('Local tracking', 'unreadable or malformed'))
        exc.recovery = 'pubanon stage repair or pubanon stage forget'
        return None, exc
    if stage is not None:
        shown_commits[stage.commit] = 'recorded staged commit'
    detail_fields(
        [
            ('Recorded destination', escape(f'{stage._owner}/{stage._repo}' if stage else 'none')),
            ('Recorded staged commit', escape(stage.commit if stage else 'none')),
        ]
    )
    return stage, None


def _status_target(
    paths: Paths, g: GlobalConfig, p: ProjectConfig, shown_commits: dict[str, str]
) -> tuple[Destination, StageRecord, PubanonError | None]:
    """Report observed staging facts and identify a snapshot without changing state."""
    dest = _stage_destination(paths, g, p)
    stage, tracking_error = _status_tracking(paths, p, shown_commits)
    require_authenticated_account(dest.account, authority='staging configuration')
    facts = repository_facts(
        dest.owner,
        dest.repo,
        missing_recovery='inspect GitHub access, then rerun pubanon stage status',
    )
    detail(field('Visibility', escape(facts[2])))
    _require_stage_facts(
        dest, facts, recovery='inspect the configured staging repository on GitHub'
    )
    heading(f'Staging repository {escape(dest.owner)}/{escape(dest.repo)} (private)')
    detail(field('Repository URL', escape(dest.url)))
    refs = read_remote_refs(dest)
    detail(
        field(
            'Observed remote commit',
            _status_commit(refs.main, 'observed remote commit', shown_commits),
        )
    )
    for branch, commit in refs.branches:
        detail(
            field(
                f'Remote branch {escape(branch)}',
                _status_commit(commit, f'remote branch {escape(branch)}', shown_commits),
            )
        )
    settings = repository_settings(dest.owner, dest.repo)
    detail(field('Default branch', escape(settings.default_branch)))
    _verify_stage_refs(refs)
    _verify_stage_default(settings.default_branch)
    if refs.main is None:
        raise _unrecognized_stage_tip(dest, None)
    observed = StageRecord(refs.main, dest.owner, dest.repo)
    record = _recognized_stage_record(paths, g, p, observed, stage)
    if record is None:
        if tracking_error is not None:
            output(field('Tracking recovery', tracking_error.recovery or 'pubanon stage repair'))
        raise _unrecognized_stage_tip(dest, refs.main)
    if stage != record and tracking_error is None:
        tracking_error = PubanonError(
            'local staging tracking does not match the identified remote snapshot',
            recovery='pubanon stage repair',
        )
    return dest, record, tracking_error


def candidate_next_action(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    record: CandidateRecord | None,
    *,
    checked_commit: str | None = None,
) -> str:
    """Suggest a local route step after snapshot recovery has been considered.

    Args:
        paths: Derived local Pubanon paths.
        g: Global configuration for snapshot fingerprint recovery.
        p: Project configuration with the selected route.
        record: Retained candidate, or `None` when preparation is needed.
        checked_commit: Current invocation's verified staging target after all
            configured required checks succeeded. Never persisted.

    Returns:
        The next command based on the saved route and local staging evidence.
    """
    if record is None:
        return 'pubanon prepare'
    recovery = inspect_next_action(record, g, p)
    if recovery != 'pubanon publish':
        return recovery
    if p.publication_route == 'direct':
        return 'pubanon publish'
    try:
        stage = load_stage(paths, p.name)
    except _InvalidStageRecordError:
        return 'pubanon stage repair'
    if stage is None:
        action = 'pubanon stage push'
    elif (stage._owner, stage._repo) != (p.owner, stage_repository(p)):
        action = 'pubanon stage repair'
    elif stage.commit != candidate_tip(record):
        action = 'pubanon stage push'
    elif p.required_checks and checked_commit != stage.commit:
        action = 'pubanon stage watch'
    else:
        action = 'pubanon publish'
    return action


def stage_delete(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    yes: bool,
) -> None:
    """Delete the exact configured private staging repository after verification.

    A malformed record does not supply coordinates. A readable mismatch refuses
    deletion. Only a successful DELETE followed by classified absence clears
    local tracking.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        yes: Whether to skip the deletion confirmation.

    Raises:
        PubanonError: Local guards, host facts, deletion, or read-back are
            unsafe.
    """
    dest = _stage_destination(paths, g, p)
    try:
        stage = load_stage(paths, p.name)
    except _InvalidStageRecordError:
        stage = None
    configured = (dest.owner, dest.repo)
    if stage is not None and (stage._owner, stage._repo) != configured:
        raise PubanonError(
            'recorded staging coordinates differ from configured destination',
            expected=f'{escape(dest.owner)}/{escape(dest.repo)}',
            actual=f'{escape(stage._owner)}/{escape(stage._repo)}',
            recovery='inspect the recorded target; use pubanon stage forget to remove tracking',
        )

    require_authenticated_account(dest.account, authority='staging configuration')
    if not repository_visible(dest.owner, dest.repo):
        raise PubanonError(
            'staging repository is absent or inaccessible before deletion',
            recovery='inspect the host; use pubanon stage forget to remove local tracking',
        )
    _verify_stage_facts(dest, recovery='inspect and delete the staging repository manually')

    heading(f'Delete staging repository {escape(dest.owner)}/{escape(dest.repo)}')
    fields([('Visibility', dest.visibility), ('Route', escape(p.publication_route))])
    detail(field('Repository URL', escape(dest.url)))
    output(
        'warning: this deletes the entire staging repository, including branches, tags, '
        'Actions history, and GitHub records.',
        kind='warning',
    )
    if not yes and not confirm(
        f'Delete the staging repository {escape(dest.owner)}/{escape(dest.repo)}?'
    ):
        raise OperationCanceled('Canceled. Nothing was deleted.')
    delete_repository(dest.owner, dest.repo)
    try:
        visible = repository_visible(dest.owner, dest.repo)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise PubanonError(
            'cannot verify staging deletion',
            fact=exc.render(),
            recovery='inspect the host before retrying deletion or explicitly forgetting tracking',
        ) from exc
    if visible:
        raise PubanonError(
            'staging deletion did not complete',
            recovery='inspect the host, then retry pubanon stage delete',
        )
    _clear_stage(paths, p.name)
    output(
        f'Deleted staging repository {escape(dest.owner)}/{escape(dest.repo)} '
        'and its local tracking.',
        kind='success',
    )
    output('The source and final repositories were not changed.', kind='note')


def stage_forget(paths: Paths, g: GlobalConfig, p: ProjectConfig, *, yes: bool) -> None:
    """Forget local staged-candidate comparison without contacting the host.

    The caller must hold the project lock for the complete operation.
    """
    del g
    if not _stage_path(paths, p.name).exists():
        output('No local staging record exists.')
        return
    heading('Forget local staging record')
    output('Forgetting tracking keeps the selected publication route.')
    output('A staged route needs staging again before publication.')
    output('Use pubanon init --publication-route direct to select direct publication.')
    output('The private staging repository is not changed.')
    if not yes and not confirm('Forget the local staging record?'):
        raise OperationCanceled('Canceled. Nothing was removed.')
    _clear_stage(paths, p.name)
    output('Forgot the local staging record.', kind='success')


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
    try:
        stage = load_stage(paths, p.name)
    except _InvalidStageRecordError:
        stage = None
        output('Malformed tracking will be backed up before replacement.', kind='warning')
    dest = _stage_destination(paths, g, p)

    require_authenticated_account(dest.account, authority='staging configuration')
    if creating_repository := not repository_visible(dest.owner, dest.repo):
        heading('Staging repository creation')
        fields(
            [
                ('Staging repository', f'{escape(dest.owner)}/{escape(dest.repo)} (private)'),
                ('Prepared snapshot', tip[:12]),
            ]
        )
        if not confirm(
            f'Create the private staging repository {escape(dest.owner)}/{escape(dest.repo)}?'
        ):
            raise OperationCanceled('Canceled. Nothing was created.')
        create_repository(
            dest,
            p.settings,
            settings_recovery=(
                "check the settings in the repository's GitHub settings, "
                'then run pubanon stage push'
            ),
        )
    _verify_stage_facts(dest, recovery='pubanon stage repair')
    refs = read_remote_refs(dest)
    _verify_stage_refs(refs)
    settings = repository_settings(dest.owner, dest.repo)
    if refs.main is not None:
        _verify_stage_default(settings.default_branch)
    expected_old = refs.main
    _report_stage_default_change(refs, settings, yes=yes)

    if not yes:
        heading('Stage prepared snapshot', separate=creating_repository)
        _preview_stage_push(dest, record, stage, refs, settings)
    if not yes and not confirm(f'Push {tip[:12]} to the staging repository?'):
        raise OperationCanceled(
            'Canceled. Nothing was pushed.',
            fact='The private staging repository was created and is kept.'
            if creating_repository
            else None,
            recovery='pubanon stage push',
        )
    _push_stage_main(dest, store_path(paths, p.name), tip, expected_old)

    try:
        _finish_stage_push(dest, tip, refs, settings)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise PubanonError(
            'cannot verify staging push read-back; '
            'remote update succeeded but tracking was not saved',
            fact=exc.render(),
            recovery='pubanon stage repair',
        ) from exc
    _record_stage_push(paths, p.name, StageRecord(tip, dest.owner, dest.repo))
    label = escape(record.tag_intent) if record.tag_intent else 'snapshot'
    output(
        f'Staged {label} ({escape(tip[:12])}) in private repository '
        f'{escape(dest.owner)}/{escape(dest.repo)}.',
        kind='success',
    )
    next_step(candidate_next_action(paths, g, p, record))


def stage_repair(paths: Paths, g: GlobalConfig, p: ProjectConfig, *, yes: bool) -> None:
    """Reconnect recognized staging state under the caller's project lock.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.
        yes: Whether to approve the previewed repair without confirmation.

    Raises:
        PubanonError: Observation or local evidence cannot establish supported
            staging state, repair is declined, or local persistence fails.
    """
    try:
        _repair_stage(paths, g, p, yes=yes)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        if exc.recovery is None:
            exc.recovery = (
                'inspect the staging repository and local evidence, '
                'then rerun pubanon stage repair'
            )
        raise


def stage_status(paths: Paths, g: GlobalConfig, p: ProjectConfig) -> None:
    """Report read-only staging facts, candidate agreement, and exact snapshot CI.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

    Raises:
        PubanonError: Tracking is unresolved or local state, host facts, refs,
            or required checks are unacceptable. Available facts are printed
            first.
    """
    shown_commits: dict[str, str] = {}
    try:
        dest, stage, tracking_error = _status_target(paths, g, p, shown_commits)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        if exc.recovery is None:
            exc.recovery = (
                'inspect staging access and local evidence, then rerun pubanon stage status'
            )
        _status_candidate(paths, p, None, shown_commits)
        raise
    _status_candidate(paths, p, stage.commit, shown_commits)
    candidate = load_candidate(paths, p.name)
    label = (
        escape(candidate.tag_intent)
        if candidate is not None
        and candidate_tip(candidate) == stage.commit
        and candidate.tag_intent
        else 'snapshot'
    )
    match = (
        ', same as the prepared snapshot'
        if candidate is not None and candidate_tip(candidate) == stage.commit
        else ''
    )
    output(field('Staged', f'{label} ({escape(stage.commit[:12])}){match}'))
    if tracking_error is not None:
        output(field('Tracking recovery', tracking_error.recovery or 'pubanon stage repair'))
    _status_checks(dest, stage, p)
    if tracking_error is not None:
        raise tracking_error
    next_step(
        candidate_next_action(
            paths,
            g,
            p,
            load_candidate(paths, p.name),
            checked_commit=stage.commit if p.required_checks else None,
        )
    )


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
        raise PubanonError(
            'no staged commit is recorded', recovery='pubanon stage repair or pubanon stage push'
        )
    configured = (p.owner, stage_repository(p))
    if (record._owner, record._repo) != configured:
        raise PubanonError(
            'configured staging coordinates differ from the recorded staging coordinates',
            expected=(f'{escape(record._owner)}/{escape(record._repo)}'),
            actual=(f'{escape(configured[0])}/{escape(configured[1])}'),
            recovery='pubanon stage repair',
        )
    destination = _stage_destination(paths, g, p)
    if not candidate_store_path(paths, p.name).is_dir():
        raise PubanonError(
            'snapshot store is absent',
            recovery='pubanon repo restore --to local',
        )
    try:
        if timeout is None:
            require_authenticated_account(destination.account, authority='staging configuration')
            _verify_stage_facts(destination, recovery='pubanon stage repair')
        else:
            require_authenticated_account(
                destination.account,
                authority='staging configuration',
                timeout=_remaining_timeout(timeout, started),
            )
            _verify_stage_facts(
                destination,
                recovery='pubanon stage repair',
                timeout=_remaining_timeout(timeout, started),
            )
    except ObservationTimeoutError as exc:
        raise ObservationTimeoutError(stage='account/repository verification') from exc
    try:
        refs = read_remote_refs(
            destination,
            timeout=None if timeout is None else _remaining_timeout(timeout, started),
        )
    except ObservationTimeoutError as exc:
        raise ObservationTimeoutError(stage='refs') from exc
    _verify_stage_refs(refs)
    if refs.main != record.commit:
        raise PubanonError(
            'staging tip does not match the recorded staged commit',
            expected=escape(record.commit),
            actual=escape(refs.main or 'absent'),
            recovery='pubanon stage repair or pubanon stage push',
        )
    try:
        settings = repository_settings(
            destination.owner,
            destination.repo,
            timeout=None if timeout is None else _remaining_timeout(timeout, started),
        )
    except ObservationTimeoutError as exc:
        raise ObservationTimeoutError(stage='repository settings verification') from exc
    _verify_stage_default(settings.default_branch)

    return destination, record
