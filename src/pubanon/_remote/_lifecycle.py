"""Create and manage the configured host repository."""

import json
import os
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import NamedTuple

from pubanon import _filesystem, _history_output
from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    Settings,
    assemble_rules,
    create_data_dirs,
    derived_private_terms,
    gate_host_coordinates,
    save_project,
    validate_settings,
)
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._gate import gate_unit
from pubanon._remote import _actions, _host, _immutable, _package, _releases, _stage, _watch
from pubanon._remote._ci import RunQuery, run_evidence
from pubanon._remote._host import (
    Destination,
    Disclosure,
    RemoteRefs,
    RepositoryCreationUncertainError,
    SettingsFacts,
)
from pubanon._snapshots import _audit
from pubanon._snapshots._candidate import (
    STATE_UNKNOWN_OUTCOME,
    published_refs,
    require_known_outcome,
)
from pubanon._snapshots._records import load_candidate
from pubanon._terminal import (
    confirm,
    detail,
    display_check_result,
    display_path,
    escape,
    fields,
    heading,
    next_step,
    output,
)

_INTERRUPTED = (
    'Interrupted. The repository may already be public; rerun pubanon repo reveal to verify '
    'and record it.'
)
_PAIR_SIZE = 2
_POST_FLIP_FAILURE_MESSAGE = 'the repository is now public but post-flip verification failed'
_POST_FLIP_POLL_SECONDS = 5
_POST_FLIP_RECOVERY = 'fix the reported drift, then rerun pubanon repo reveal'
_POST_FLIP_WAIT_MINUTES = 3
_SECONDS_PER_MINUTE = 60
_monotonic = time.monotonic
_sleep = time.sleep


class _HistoryReview(NamedTuple):
    """Carry one pre-reveal history audit to the reveal decision."""

    audit: _audit.HistoryAudit
    commits: tuple[str, ...]
    tags: dict[str, str]
    derived: tuple[str, ...]


class _PostFlipReadError(PubanonError):
    """Report a post-flip read that failed, rather than a difference it observed."""


def _ensure_candidate_outcome_known(paths: Paths, project: str) -> None:
    """Refuse reveal while a candidate publication outcome is unknown."""
    candidate = load_candidate(paths, project)
    require_known_outcome(candidate)


def _joined(names: Sequence[str]) -> str:
    """Join names as `a`, `a and b`, or `a, b, and c`."""
    if len(names) <= _PAIR_SIZE:
        return ' and '.join(names)
    return ', '.join(names[:-1]) + f', and {names[-1]}'


def _immutable_advice(tags: Sequence[str]) -> str:
    """Explain what immutable release pages block when correcting history."""
    names = _joined([escape(tag) for tag in tags])
    if len(tags) == 1:
        return (
            f'Release page {names} is immutable: pubanon cannot replace history that moves its '
            'tag, and a recreated repository cannot reuse that version.'
        )
    return (
        f'Release pages {names} are immutable: pubanon cannot replace history that moves their '
        'tags, and a recreated repository cannot reuse those versions.'
    )


def _decide_history(
    configs: tuple[GlobalConfig, ProjectConfig],
    review: _HistoryReview,
    *,
    already_public: bool,
    immutable: Sequence[str] = (),
) -> str:
    """Ask whether history findings may be public and return the accepted digest."""
    if not review.audit.findings:
        return ''
    for line in _history_output.finding_summary(
        review.audit, configs, review.derived, review.commits, review.tags
    ):
        output(line)
    if already_public:
        output(
            'These matches are already public. Replacing published history hides them from '
            'new clones only; existing clones, forks, and caches keep them.'
        )
        question = 'Record the repository as public with this history?'
    else:
        release = _history_output.earliest_release(review.audit, review.commits, review.tags)
        output('Making the repository public publishes these matches.')
        output(
            f'{_history_output.correction_text(release)} Recreating removes release pages, '
            'Actions history, and the Activity log; it does not change packages or other '
            'copies published elsewhere.'
        )
        question = 'Make these public with the repository?'
    if immutable:
        output(_immutable_advice(immutable))
    if not confirm(question):
        raise OperationCanceled('Canceled. Nothing was changed.')
    return _audit.exposure_digest(review.audit.findings)


def _require_configured_coordinates(
    p: ProjectConfig,
    owner: str,
    repo: str,
) -> None:
    """Require observed repository coordinates to match configuration."""
    if (owner, repo) != (p.owner, p.repo):
        raise PubanonError(
            'repository coordinates do not match configuration',
            fact='repository coordinates',
            expected=(f'{escape(p.owner)}/{escape(p.repo)}'),
            actual=(f'{escape(owner)}/{escape(repo)}'),
            recovery='pubanon repo verify',
        )


def _post_flip_repository_facts(p: ProjectConfig) -> tuple[str, str, str]:
    """Read repository facts while distinguishing a failed read from coordinate drift."""
    try:
        owner, repo, visibility = _host.repository_facts(p.owner, p.repo)
    except PubanonError as exc:
        raise _PostFlipReadError(
            _POST_FLIP_FAILURE_MESSAGE,
            fact=exc.render(),
            recovery=_POST_FLIP_RECOVERY,
        ) from exc
    try:
        _require_configured_coordinates(p, owner, repo)
    except PubanonError as exc:
        raise PubanonError(
            _POST_FLIP_FAILURE_MESSAGE,
            fact=exc.render(),
            recovery=_POST_FLIP_RECOVERY,
        ) from exc
    return owner, repo, visibility


def _require_supported_settings(paths: Paths, p: ProjectConfig) -> None:
    """Refuse unsupported settings before any host interaction."""
    if p.settings is None:
        raise PubanonError('repository settings are missing')
    validate_settings(p.settings, project_file=paths.config_dir / 'projects' / f'{p.name}.toml')


def _save_disclosure(
    paths: Paths,
    project: str,
    inventory: Disclosure,
) -> Path:
    """Write one complete disclosure inventory with owner-only POSIX permissions."""
    path = create_data_dirs(paths, project) / 'disclosure.json'
    payload = {
        'runs': [escape(f'{entry.identifier} {entry.name}') for entry in inventory.runs],
        'artifacts': [escape(f'{entry.identifier} {entry.name}') for entry in inventory.artifacts],
    }
    try:
        path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        _filesystem.chmod(path, 0o600)
    except (OSError, UnicodeError) as exc:
        raise PubanonError('cannot write complete disclosure list') from exc
    return path


def _confirm_disclosure(
    paths: Paths,
    p: ProjectConfig,
    deleted: Sequence[int],
    *,
    already_public: bool,
) -> None:
    """Save the inventory, name what stays unchecked, and ask the final question."""
    inventory = _host.disclosure(p.owner, p.repo)
    path = _save_disclosure(paths, p.name, inventory)
    caches = _host.actions_cache_count(p.owner, p.repo)
    _actions.show_unchecked(
        (p.owner, p.repo),
        [entry.identifier for entry in inventory.runs],
        artifacts=len(inventory.artifacts),
        caches=caches,
    )
    detail(f'Saved inventory: {display_path(path)}')
    repository = f'{escape(p.owner)}/{escape(p.repo)}'
    question = f'Record {repository} as public?' if already_public else 'Make it public?'
    canceled = _actions.cancel_message(deleted)
    try:
        accepted = confirm(question)
    except OperationCanceled:
        raise OperationCanceled(canceled) from None
    if not accepted:
        raise OperationCanceled(canceled)


def _set_public_visibility(p: ProjectConfig) -> None:
    """Flip visibility while preserving an honest unknown-outcome boundary."""
    try:
        _host.set_visibility(p.owner, p.repo, 'public')
    except PubanonError as exc:
        raise PubanonError(
            'repository visibility outcome is unknown; the repository may already be public',
            fact=exc.render(),
            recovery='rerun pubanon repo reveal',
        ) from exc


def _verify_checks(p: ProjectConfig, commit: str) -> str:
    """Verify configured CI evidence for the exact release commit."""
    if p.ci_workflow:
        evidence = run_evidence(
            (p.owner, p.repo),
            RunQuery(p.ci_workflow, commit, 'push'),
            required=p.required_checks,
        )
        if evidence.state != 'success':
            raise PubanonError(
                'the CI run is not acceptable for the release commit',
                fact=escape(evidence.detail) if evidence.detail else evidence.state,
                recovery='pubanon repo watch, then rerun pubanon repo reveal',
            )
        return f'passed (latest run of {escape(p.ci_workflow)})'
    if not p.required_checks:
        return 'none configured'
    verdicts = [_host.check_result((p.owner, p.repo), commit, name) for name in p.required_checks]
    failing = [verdict for verdict in verdicts if verdict.state != 'success']
    if failing:
        raise PubanonError(
            'required checks are not acceptable for the release commit',
            fact=', '.join(
                f'{escape(item.name)}: {display_check_result(item)}' for item in failing
            ),
            recovery=_host.check_recovery(failing, retry='rerun `pubanon repo reveal`'),
        )
    return 'passed'


def _verify_existing_empty_repository(paths: Paths, g: GlobalConfig, p: ProjectConfig) -> None:
    """Verify a pre-created destination without changing it or recording history."""
    dest = _host.verify_destination(paths, g, p)
    if _host.read_remote_refs(dest) != RemoteRefs(None, {}, ()):
        commit, tags = published_refs(paths, p.name)
        raise PubanonError(
            'configured repository already contains branches or tags',
            recovery=(
                'pubanon repo verify'
                if commit is not None or tags
                else 'use an empty repository for first publication; '
                'if recovering published history from this repository, '
                'run pubanon repo restore --to local'
            ),
        )
    if p.settings is None:
        raise PubanonError('repository settings are missing')
    mismatches = _host.settings_differences(
        p.settings,
        _host.repository_settings(p.owner, p.repo),
        skip_default_branch=True,
    )
    if mismatches:
        raise PubanonError(
            'repository settings do not match configuration',
            fact='; '.join(escape(item) for item in mismatches),
            recovery=(
                "correct the repository's GitHub settings or saved configuration, "
                'then rerun pubanon repo create'
            ),
        )


def _verify_history(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    published: tuple[str, dict[str, str]],
) -> _HistoryReview:
    """Audit all published history under current rules and refuse identity problems."""
    commit, tags = published
    commits = _audit.published_commits(paths, p.name, new=commit)
    derived = derived_private_terms(paths, p, os.environ)
    rules = assemble_rules(g, p, derived)
    result = _audit.audit_history(
        paths, p.name, commits, tags, rules, f'{g.anon_name} <{g.anon_email}>', tip=commit
    )
    if result.identity_problems:
        raise PubanonError(
            'published history has identity problems',
            fact='\n'.join(escape(problem) for problem in result.identity_problems),
            recovery=(
                'pubanon audit shows the problems; to replace published history under the '
                'current identity, use `pubanon prepare --replace all`'
            ),
        )
    return _HistoryReview(result, commits, tags, derived)


def _verify_package_observation(
    paths: Paths,
    p: ProjectConfig,
    dest: Destination,
) -> str:
    """Verify one configured package release without waiting or mutation."""
    package = p.package
    if package is None:
        return 'not enabled'

    version, commit = _watch.select_package_release(paths, p.name, None)
    verdicts = _watch.ci_verdicts(dest, commit, package.release_checks)
    failing = [verdict for verdict in verdicts if verdict.state != 'success']
    bad = [verdict for verdict in failing if verdict.state != 'pending']
    selected = bad or failing
    if selected:
        fact = ', '.join(
            f'{escape(verdict.name)}: {display_check_result(verdict)}' for verdict in selected
        )
        recovery = _host.check_recovery(selected, retry='run `pubanon package watch`')
        if bad:
            raise PubanonError(
                'package release checks are not acceptable',
                fact=fact,
                recovery=recovery,
            )
        raise PubanonError(
            'package release checks are pending',
            fact=fact,
            recovery=recovery,
        )

    try:
        release = _package.release_state(package.index, package.name, version)
    except ObservationTimeoutError as exc:
        raise PubanonError(
            'package release observation timed out',
            recovery='rerun pubanon repo reveal to retry package observation',
        ) from exc
    if release is None:
        raise PubanonError(
            'package release is absent',
            fact=(f'{escape(package.name)} {escape(version)} is not published'),
            recovery='run the project release workflow, then rerun `pubanon repo reveal`',
        )
    if not release.files:
        raise PubanonError(
            'package release has no distribution files',
            fact=(f'{escape(package.name)} {escape(version)} metadata has no distribution files'),
            recovery='run `pubanon package watch`, then rerun `pubanon repo reveal`',
        )
    return f'{escape(package.name)} {escape(version)} is published'


def _verify_refs(
    paths: Paths,
    p: ProjectConfig,
    dest: Destination,
) -> tuple[str, dict[str, str]]:
    """Verify exact local and host reference equality."""
    local_main, local_tags = published_refs(paths, p.name)
    expected = RemoteRefs(local_main, local_tags, ())
    remote = _host.ls_remote(dest, paths, p.name)
    if local_main is None:
        recovery = 'pubanon repo restore --to local' if remote != expected else None
        raise PubanonError(
            'nothing is published',
            expected=escape(_host.remote_refs_text(expected)),
            actual=escape(_host.remote_refs_text(remote)),
            recovery=recovery,
        )
    if remote != expected:
        raise PubanonError(
            'GitHub branches and tags do not match saved published history',
            expected=escape(_host.remote_refs_text(expected)),
            actual=escape(_host.remote_refs_text(remote)),
            recovery='pubanon repo verify',
        )
    return local_main, local_tags


def _verify_release_pages(
    paths: Paths, g: GlobalConfig, p: ProjectConfig, dest: Destination
) -> tuple[str, tuple[str, ...]]:
    """Gate every release page; return the table text and immutable tags."""
    pages = _host.all_releases(dest)
    rules = assemble_rules(g, p, derived_private_terms(paths, p, os.environ))
    blocked = [
        f'{escape(page.tag)}{" (draft)" if page.draft else ""}: {finding}'
        for page in pages
        for finding in _releases.release_metadata_findings(
            rules, tag=page.tag, title=page.title, notes=page.notes.encode()
        )
    ]
    if blocked:
        raise PubanonError(
            'release pages are blocked',
            fact='\n'.join(blocked),
            recovery='edit or delete the listed release pages on GitHub, '
            'then rerun pubanon repo reveal',
        )
    immutable = tuple(
        page.tag
        for page in sorted(pages, key=lambda page: (page.created_at, page.identifier))
        if page.immutable and not page.draft
    )
    return ('no sensitive terms' if pages else 'none'), immutable


def feature_summary(settings: Settings) -> str:
    """Summarize enabled features before disabled features for display."""
    features = (
        ('issues', settings.issues),
        ('pull requests', settings.pull_requests),
        ('discussions', settings.discussions),
        ('projects', settings.projects),
        ('wiki', settings.wiki),
    )
    enabled = [name for name, value in features if value]
    disabled = [name for name, value in features if not value]

    def _names(names: list[str]) -> str:
        if not names:
            return 'none'
        first, *rest = names
        if not rest:
            return first
        if len(rest) == 1:
            return f'{first} and {rest[0]}'
        return ', '.join(names[:-1]) + f', and {names[-1]}'

    return f'{_names(enabled)} on; {_names(disabled)} off'


def _preview_create(p: ProjectConfig, visibility: str) -> None:
    """Print the exact configured repository creation plan."""
    heading(f'Create repository {escape(p.owner)}/{escape(p.repo)}')
    if p.settings is None:
        raise PubanonError('repository settings are missing')
    fields(
        [
            ('Visibility', escape(visibility)),
            ('Description', escape(p.settings.description or '(empty)')),
            ('Default branch', escape(p.settings.default_branch)),
            ('Features', feature_summary(p.settings)),
        ]
    )


def gate_host_fields(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> None:
    """Gate configured repository description and default branch metadata.

    Args:
        paths: Derived pubanon configuration and data paths.
        g: Global configuration.
        p: Project configuration whose settings are gated.

    Raises:
        PubanonError: Repository settings are missing, or the description or
            default branch matches an active content rule. The diagnostic
            identifies the matching value.
    """
    if p.settings is None:
        raise PubanonError('repository settings are missing')
    rules = assemble_rules(g, p, derived_private_terms(paths, p, os.environ))
    fields = (
        ('description', p.settings.description),
        ('default branch', p.settings.default_branch),
    )
    for name, value in fields:
        unit = f'repository {name}'
        hits = gate_unit(unit, value.encode(), rules)
        if hits:
            raise PubanonError(
                f'{unit} did not pass content checks',
                fact=f'{name}: {escape(value)}',
            )


def repo_create(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    yes: bool,
) -> None:
    """Create or independently verify the configured empty repository.

    The caller must hold the project lock for the complete operation.
    An existing empty repository is checked without host mutations.

    Args:
        paths: Derived pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        yes: Whether to skip the creation confirmation.

    Raises:
        PubanonError: A disclosure gate, host precondition, creation, or
            postcondition fails. A failed creation or postcondition may leave a
            repository on the host.
    """
    _require_supported_settings(paths, p)
    candidate = load_candidate(paths, p.name)
    gate_host_coordinates(paths, g, p, coordinates=(p.owner, p.repo))
    gate_host_fields(paths, g, p)

    account = _host.require_authenticated_account(g.accounts[p.host], authority='configuration')
    if _host.repository_visible(p.owner, p.repo):
        _verify_existing_empty_repository(paths, g, p)
        output(
            'Repository already exists and is empty; configuration matches.',
            kind='success',
        )
        if candidate is not None and candidate.state == STATE_UNKNOWN_OUTCOME:
            output('The pending publication outcome needs resolution.')
        next_step(_stage.candidate_next_action(paths, g, p, candidate))
        return

    dest = Destination(
        adapter=p.host,
        owner=p.owner,
        repo=p.repo,
        url=_host.repository_url(p.owner, p.repo),
        account=account,
        visibility=p.visibility,
    )
    _preview_create(p, p.visibility)
    if not yes and not confirm(
        f'Create {escape(p.owner)}/{escape(p.repo)} as a {p.visibility} repository?'
    ):
        raise OperationCanceled('Canceled. Nothing was created.')
    try:
        _host.create_repository(
            dest,
            p.settings,
            settings_recovery=(
                "check the settings in the repository's GitHub settings, "
                'then run pubanon repo create'
            ),
        )
    except RepositoryCreationUncertainError as exc:
        raise PubanonError(
            'repository creation outcome is unknown; the repository may already exist',
            fact=exc.render(),
            recovery='pubanon repo create',
        ) from exc

    try:
        _host.verify_destination(paths, g, p)
        remote = _host.ls_remote(dest, paths, p.name)
    except PubanonError as exc:
        raise PubanonError(
            'repository creation is partial; post-create verification failed',
            fact=exc.render(),
            recovery='pubanon repo create',
        ) from exc
    if remote != RemoteRefs(None, {}, ()):
        raise PubanonError(
            'repository creation is partial; created repository is not empty',
            recovery='pubanon repo create',
        )
    output(
        f'Created {escape(p.visibility)} repository {escape(p.owner)}/{escape(p.repo)}.',
        kind='success',
    )
    output()
    if candidate is not None and candidate.state == STATE_UNKNOWN_OUTCOME:
        output('The pending publication outcome needs resolution.')
    next_step(_stage.candidate_next_action(paths, g, p, candidate))


def settings_mismatches(expected: Settings, actual: SettingsFacts) -> list[str]:
    """Compare every declared setting with observed repository facts.

    Args:
        expected: Settings declared in the project configuration.
        actual: Settings observed from the host repository.

    Returns:
        One ordered diagnostic for each mismatched setting.
    """
    return _host.settings_differences(expected, actual)


def _verify_after_flip(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> str:
    """Verify the public repository and return its published commit."""

    def _partial_failure(
        *,
        fact: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
        unread: bool = False,
    ) -> PubanonError:
        error_class = (PubanonError, _PostFlipReadError)[unread]
        return error_class(
            _POST_FLIP_FAILURE_MESSAGE,
            fact=fact,
            expected=expected,
            actual=actual,
            recovery=_POST_FLIP_RECOVERY,
        )

    if p.settings is None:
        raise _partial_failure(fact='repository settings are missing')

    owner, repo, visibility = _post_flip_repository_facts(p)
    if visibility != 'public':
        raise _partial_failure(
            fact='repository visibility',
            expected='public',
            actual=escape(visibility),
        )

    destination = Destination(
        adapter=p.host,
        owner=owner,
        repo=repo,
        url=_host.repository_url(owner, repo),
        account=g.accounts[p.host],
        visibility=visibility,
    )
    try:
        local_main, local_tags = published_refs(paths, p.name)
    except PubanonError as exc:
        raise _partial_failure(fact=exc.render()) from exc
    expected_refs = RemoteRefs(local_main, local_tags, ())
    try:
        remote = _host.read_remote_refs(destination)
    except PubanonError as exc:
        raise _partial_failure(fact=exc.render(), unread=True) from exc
    if remote != expected_refs:
        raise _partial_failure(
            fact='published refs changed after the visibility flip',
            expected=escape(_host.remote_refs_text(expected_refs)),
            actual=escape(_host.remote_refs_text(remote)),
        )

    try:
        actual_settings = _host.repository_settings(p.owner, p.repo)
    except PubanonError as exc:
        raise _partial_failure(fact=exc.render(), unread=True) from exc
    mismatches = settings_mismatches(p.settings, actual_settings)
    if mismatches:
        raise _partial_failure(fact='; '.join(escape(item) for item in mismatches))
    return local_main or 'absent'


def _verify_after_visibility_change(paths: Paths, g: GlobalConfig, p: ProjectConfig) -> str:
    """Verify after reveal's own flip, rereading while GitHub finishes applying it."""
    try:
        return _verify_after_flip(paths, g, p)
    except _PostFlipReadError as exc:
        failure = exc
    started = _monotonic()
    output('The repository is public, but pubanon could not read it back yet.')
    output(
        'GitHub may still be applying the change. '
        f'Retrying for up to {_POST_FLIP_WAIT_MINUTES} minutes.'
    )
    limit = _POST_FLIP_WAIT_MINUTES * _SECONDS_PER_MINUTE
    while _monotonic() - started + _POST_FLIP_POLL_SECONDS <= limit:
        _sleep(_POST_FLIP_POLL_SECONDS)
        if _monotonic() - started > limit:
            break
        try:
            return _verify_after_flip(paths, g, p)
        except _PostFlipReadError as exc:
            failure = exc
            elapsed = round(_monotonic() - started)
            detail(f'After {elapsed} s: {escape(str(exc.__cause__ or exc))}')
    raise PubanonError(
        'the repository is now public, but GitHub may still be applying the change',
        fact=(
            f'pubanon could not read the repository back within {_POST_FLIP_WAIT_MINUTES} '
            f'minutes.\n{failure.fact}'
        ),
        recovery='wait a minute, then rerun pubanon repo reveal',
    ) from failure


def _converge_public(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    host_changed: bool,
    accepted_exposure: str = '',
) -> None:
    """Verify and persist one completed public-state transition."""
    commit = (
        _verify_after_visibility_change(paths, g, p)
        if host_changed
        else _verify_after_flip(paths, g, p)
    )
    updated = replace(p, visibility='public')
    if accepted_exposure:
        updated = replace(updated, acknowledged_exposure=accepted_exposure)
    try:
        save_project(paths, updated)
    except PubanonError as exc:
        raise PubanonError(
            'the repository is public and verified but configuration update failed',
            fact=exc.render(),
            recovery='fix the configuration write, then rerun pubanon repo reveal',
        ) from exc
    repository = f'{escape(p.owner)}/{escape(p.repo)}'
    output(
        f'Made {repository} public.'
        if host_changed
        else f'Recorded public visibility for {repository}; it was already public.',
        kind='success',
    )
    detail(f'Verified commit: {escape(commit)}')


def _verify_settings(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> None:
    """Gate and compare every declared public repository setting."""
    if p.settings is None:
        raise PubanonError('repository settings are missing')
    gate_host_fields(paths, g, p)
    mismatches = settings_mismatches(
        p.settings,
        _host.repository_settings(p.owner, p.repo),
    )
    if mismatches:
        raise PubanonError(
            'repository settings do not match configuration',
            fact='; '.join(escape(item) for item in mismatches),
        )


def _verify_before_flip(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    dest: Destination,
    *,
    already_public: bool = False,
) -> tuple[str, str]:
    """Verify, ask, and recheck before recording public visibility; return commit and digest."""
    commit, tags = _verify_refs(paths, p, dest)
    checks = _verify_checks(p, commit)
    package = _verify_package_observation(paths, p, dest)
    _verify_settings(paths, g, p)
    review = _verify_history(paths, g, p, (commit, tags))
    pages, immutable = _verify_release_pages(paths, g, p, dest)
    account = g.accounts[p.host]
    actions = _actions.check((p.owner, p.repo), account, assemble_rules(g, p, review.derived))
    history = (
        'matches GitHub; findings listed below'
        if review.audit.findings
        else 'matches GitHub; no sensitive terms or identity problems'
    )
    fields(
        [
            ('Published history', history),
            ('Release pages', pages),
            ('Actions logs', _actions.row(actions)),
            ('Required checks', checks),
            ('Package', package),
            ('Settings', 'match your configuration'),
        ]
    )
    accepted = _decide_history((g, p), review, already_public=already_public, immutable=immutable)
    deleted = _actions.decide(
        actions, (g, p), review.derived, account, already_public=already_public
    )
    try:
        _confirm_disclosure(paths, p, deleted, already_public=already_public)
        _actions.require_unchanged(actions)
    except KeyboardInterrupt:
        raise OperationCanceled(_actions.cancel_message(deleted)) from None
    except OperationCanceled:
        raise
    except PubanonError as exc:
        if deleted:
            note = _actions.deletion_note(deleted)
            exc.fact = f'{exc.fact}\n{note}' if exc.fact else note
        raise
    return commit, accepted


def reveal(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> None:
    """Reveal or reverify the configured repository from durable facts.

    Before recording a private-to-public transition, verify published refs,
    checks, optional package state, settings, history, release pages, and
    Actions content. Ask before publishing history findings, offer to delete
    matching Actions logs, name what stays unchecked, and ask the final
    question. Save the accepted digest with public visibility after
    verification. If host and configuration are already public, recheck refs
    and settings without repeating the full pre-reveal audit. After recording
    public visibility, offer once to turn on immutable releases.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

    Raises:
        PubanonError: Any candidate, identity, phase, verification,
            confirmation, mutation, or configuration update fails. The host may
            already be public when a mutation or later step fails; no rollback
            is performed.
    """
    _require_supported_settings(paths, p)
    _ensure_candidate_outcome_known(paths, p.name)
    gate_host_coordinates(paths, g, p, coordinates=(p.owner, p.repo))

    account = _host.require_authenticated_account(g.accounts[p.host], authority='configuration')
    owner, repo, host_visibility = _host.repository_facts(p.owner, p.repo)
    _require_configured_coordinates(p, owner, repo)

    if host_visibility == 'private' and p.visibility == 'private':
        destination = _host.verify_destination(paths, g, p)
        heading(f'Make {escape(owner)}/{escape(repo)} public')
        _commit, accepted = _verify_before_flip(paths, g, p, destination)
        try:
            _set_public_visibility(p)
            _converge_public(paths, g, p, host_changed=True, accepted_exposure=accepted)
        except KeyboardInterrupt:
            raise OperationCanceled(_INTERRUPTED) from None
        _immutable.offer(p, destination)
        return

    if host_visibility == 'public' and p.visibility == 'private':
        heading(f'Verify public repository {escape(owner)}/{escape(repo)}')
        output(
            'warning: repository content is already public; '
            'verification cannot undo that exposure.',
            kind='warning',
        )
        destination = Destination(
            adapter=p.host,
            owner=owner,
            repo=repo,
            url=_host.repository_url(owner, repo),
            account=account,
            visibility=host_visibility,
        )
        try:
            _commit, accepted = _verify_before_flip(paths, g, p, destination, already_public=True)
        except OperationCanceled:
            raise
        except PubanonError as exc:
            raise PubanonError(
                'the repository is already public but verification failed',
                fact=exc.render(),
                recovery='fix the reported issue, then rerun pubanon repo reveal',
            ) from exc
        _converge_public(paths, g, p, host_changed=False, accepted_exposure=accepted)
        _immutable.offer(p, destination)
        return

    if host_visibility == 'public' and p.visibility == 'public':
        commit = _verify_after_flip(paths, g, p)
        output(f'Repository {escape(owner)}/{escape(repo)} is already public.', kind='success')
        output(
            'Verified now: public visibility, published branches and tags, '
            'and repository settings.'
        )
        detail(f'Verified commit: {escape(commit)}')
        output('Apply current privacy rules with a remote audit.')
        next_step('pubanon audit --remote')
        return

    if host_visibility == 'private' and p.visibility == 'public':
        project_file = paths.config_dir / 'projects' / f'{p.name}.toml'
        raise PubanonError(
            'declared visibility is public but the final repository is private',
            recovery=(
                'decide the intended visibility; make the repository public outside pubanon '
                'or set '
                f'visibility = "private" in {display_path(project_file)}, then rerun '
                'pubanon repo reveal'
            ),
        )

    raise PubanonError(
        'repository visibility state is unsupported',
        fact='host repository visibility',
        expected='private or public',
        actual=escape(host_visibility),
        recovery='pubanon repo verify',
    )
