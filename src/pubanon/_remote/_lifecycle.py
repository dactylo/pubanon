"""Create and manage the configured host repository."""

import json
import os
from collections import Counter
from dataclasses import replace
from pathlib import Path

from pubanon import _filesystem
from pubanon._config import (
    VISIBILITIES,
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
from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._gate import gate_unit
from pubanon._remote import _package, _watch
from pubanon._remote._host import (
    URL_TEMPLATE,
    Destination,
    Disclosure,
    RemoteRefs,
    RepositoryCreationUncertainError,
    SettingsFacts,
    check_recovery,
    check_result,
    create_repository,
    disclosure,
    ls_remote,
    remote_refs_text,
    repository_facts,
    repository_settings,
    repository_visible,
    require_authenticated_account,
    set_visibility,
    verify_destination,
)
from pubanon._snapshots import _audit
from pubanon._snapshots._candidate import STATE_UNKNOWN_OUTCOME, load_candidate, published_refs
from pubanon._terminal import confirm, escape, heading, next_step, output, style


def _ensure_candidate_outcome_known(paths: Paths, project: str) -> None:
    """Refuse reveal while a candidate publication outcome is unknown."""
    candidate = load_candidate(paths, project)
    if candidate is not None and candidate.state == STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'snapshot has an unknown publish outcome',
            recovery='pubanon publish --resolve',
        )


def _preview_create(p: ProjectConfig, visibility: str) -> None:
    """Print the exact configured repository creation plan."""
    heading('Repository creation preview')
    output(f'Create {escape(visibility)} GitHub repository {escape(p.owner)}/{escape(p.repo)}.')
    if p.settings is None:
        raise PubanonError('repository settings are missing')
    output(f'  {style("Description:")} {escape(p.settings.description)}')
    output(f'  The default branch is {escape(p.settings.default_branch)}.')
    output(f'  {style("Issues:")} {"enabled" if p.settings.issues else "disabled"}')
    output(f'  {style("Wiki:")} {"enabled" if p.settings.wiki else "disabled"}')
    output(f'  {style("Projects:")} {"enabled" if p.settings.projects else "disabled"}')


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


def _require_supported_settings(paths: Paths, p: ProjectConfig) -> None:
    """Refuse unsupported settings before any host interaction."""
    if p.settings is None:
        raise PubanonError('repository settings are missing')
    try:
        validate_settings(p.settings)
    except PubanonError as exc:
        if str(exc) != 'settings.default_branch must be main':
            raise
        project_file = paths.config_dir / 'projects' / f'{p.name}.toml'
        raise PubanonError(
            str(exc),
            recovery=(f'set settings.default_branch = "main" in {escape(str(project_file))}'),
        ) from None


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
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise PubanonError('cannot write complete disclosure list') from exc
    return path


def _set_public_visibility(p: ProjectConfig) -> None:
    """Flip visibility while preserving an honest unknown-outcome boundary."""
    try:
        set_visibility(p.owner, p.repo, 'public')
    except PubanonError as exc:
        raise PubanonError(
            'repository visibility outcome is unknown; the repository may already be public',
            fact=exc.render(),
            recovery='rerun pubanon repo reveal',
        ) from exc


def _setting_value(value: str | bool) -> str:
    """Render one repository setting for comparison."""
    return str(value).lower() if isinstance(value, bool) else value


def _verify_checks(p: ProjectConfig, commit: str) -> None:
    """Verify every configured check for the exact release commit."""
    if not p.required_checks:
        output(f'{style("required checks:")} none configured')
        return
    verdicts = [check_result((p.owner, p.repo), commit, name) for name in p.required_checks]
    failing = [verdict for verdict in verdicts if verdict.state != 'success']
    if failing:
        raise PubanonError(
            'required checks are not acceptable for the release commit',
            fact=', '.join(f'{escape(item.name)}: {escape(item.state)}' for item in failing),
            recovery=check_recovery(failing, retry='rerun `pubanon repo reveal`'),
        )
    output('required checks: all succeeded', kind='success')


def _verify_disclosure(
    paths: Paths,
    p: ProjectConfig,
    *,
    already_public: bool,
) -> None:
    """Persist, summarize, and acknowledge the complete disclosure list."""
    inventory = disclosure(p.owner, p.repo)
    path = _save_disclosure(paths, p.name, inventory)
    run_counts = Counter(entry.name for entry in inventory.runs)
    run_label = 'workflow run' if len(inventory.runs) == 1 else 'workflow runs'
    artifact_label = 'artifact' if len(inventory.artifacts) == 1 else 'artifacts'
    output('Git history passed the current content and identity checks.', kind='success')
    heading('Actions inventory', separate=True)
    output(
        f'Actions inventory: {len(inventory.runs)} {run_label} and '
        f'{len(inventory.artifacts)} {artifact_label}.'
    )
    for name, count in sorted(run_counts.items()):
        output(f'  {style(escape(name) + ":")} {count}')
    output(style('Artifacts:'))
    for entry in inventory.artifacts:
        output(f'  {escape(entry.name)} (id {entry.identifier})')
    if not inventory.artifacts:
        output('  none')
    output('Pubanon has not scanned their logs or artifact contents.', kind='warning')
    review_links = {
        f'Workflow run link {index}': (
            f'https://github.com/{p.owner}/{p.repo}/actions/runs/{entry.identifier}'
        )
        for index, entry in enumerate(inventory.runs, start=1)
    }
    actions_url = f'https://github.com/{p.owner}/{p.repo}/actions'
    review_links['Artifact review'] = actions_url
    for label, url in review_links.items():
        output(f'  {style(label + ":")} {url}')
    output(f'{style("Saved inventory:")} {escape(str(path))}')
    prompt = (
        'These runs, logs, and artifacts are already public. Continue?'
        if already_public
        else 'These runs, logs, and artifacts become public with the repository. Continue?'
    )
    if not confirm(prompt):
        raise PubanonError('public disclosure was not acknowledged')


def _verify_history(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    published: tuple[str, dict[str, str]],
) -> None:
    """Verify all published history under current sanitization rules."""
    commit, tags = published
    commits = _audit.published_commits(paths, p.name, new=commit)
    rules = assemble_rules(g, p, derived_private_terms(paths, p, os.environ))
    result = _audit.audit_history(paths, p.name, commits, tags, rules, g, tip=commit)
    if result.findings or result.identity_problems:
        details = _audit.history_details(result, g, p, derived_private_terms(paths, p, os.environ))
        findings = [escape(detail) for detail in details.values()]
        raise PubanonError(
            'published history failed sanitization and identity verification',
            fact='; '.join(
                [
                    *findings,
                    *(escape(problem) for problem in result.identity_problems),
                ]
            ),
            recovery=(
                'pubanon status --audit shows the findings; adjust the configuration or the '
                'published content decision. To replace all published history under the current '
                'publication identity, use `pubanon prepare --replace all`; existing clones and '
                'caches may retain the old history'
            ),
        )
    output('sanitization and identity: clean', kind='success')


def _verify_package_observation(
    paths: Paths,
    p: ProjectConfig,
    dest: Destination,
) -> None:
    """Verify one configured package release without waiting or mutation."""
    package = p.package
    if package is None:
        output(f'{style("package observation:")} not enabled')
        return

    version, commit = _watch.select_package_release(paths, p.name, None)
    verdicts = _watch.ci_verdicts(dest, commit, package.release_checks)
    failing = [verdict for verdict in verdicts if verdict.state != 'success']
    bad = [verdict for verdict in failing if verdict.state != 'pending']
    selected = bad or failing
    if selected:
        fact = ', '.join(
            f'{escape(verdict.name)}: {escape(verdict.state)} ({escape(verdict.detail)})'
            for verdict in selected
        )
        recovery = check_recovery(selected, retry='run `pubanon watch package`')
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
            recovery='run `pubanon watch package`, then rerun `pubanon repo reveal`',
        )
    output(
        f'package observation: {escape(package.name)} {escape(version)} is published',
        kind='success',
    )


def _verify_refs(
    paths: Paths,
    p: ProjectConfig,
    dest: Destination,
) -> tuple[str, dict[str, str]]:
    """Verify exact local and host reference equality."""
    local_main, local_tags = published_refs(paths, p.name)
    expected = RemoteRefs(local_main, local_tags, ())
    remote = ls_remote(dest, paths, p.name)
    if local_main is None:
        recovery = 'pubanon repo verify --repair-store' if remote != expected else None
        raise PubanonError(
            'nothing is published',
            expected=escape(remote_refs_text(expected)),
            actual=escape(remote_refs_text(remote)),
            recovery=recovery,
        )
    if remote != expected:
        raise PubanonError(
            'host refs do not match published refs',
            expected=escape(remote_refs_text(expected)),
            actual=escape(remote_refs_text(remote)),
            recovery='pubanon repo verify',
        )
    output('published refs match host', kind='success')
    return local_main, local_tags


def gate_host_fields(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> None:
    """Gate configured repository description and default branch metadata.

    Args:
        paths: Derived Pubanon configuration and data paths.
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
                f'{unit} failed content gates',
                fact=f'{name}: {escape(value)}',
            )


def repo_create(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    visibility: str,
    yes: bool,
) -> None:
    """Create and independently verify the configured empty repository.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        visibility: Explicit operator-selected visibility, which must match the
            project configuration.
        yes: Whether to skip the creation confirmation.

    Raises:
        PubanonError: A disclosure gate, host precondition, creation, or
            postcondition fails. A failed creation or postcondition may leave a
            repository on the host.
    """
    _require_supported_settings(paths, p)
    candidate = load_candidate(paths, p.name)
    if visibility not in VISIBILITIES or visibility != p.visibility:
        raise PubanonError(
            'requested repository visibility does not match configuration',
            fact='repository visibility',
            expected=escape(p.visibility),
            actual=escape(visibility),
            recovery=(
                'align the --private/--public flag with visibility in the project configuration'
            ),
        )
    gate_host_coordinates(paths, g, p, coordinates=(p.owner, p.repo))
    gate_host_fields(paths, g, p)

    account = require_authenticated_account(g.accounts[p.host], authority='configuration')
    if repository_visible(p.owner, p.repo):
        raise PubanonError(
            'configured repository already exists',
            recovery='pubanon repo verify',
        )

    dest = Destination(
        adapter=p.host,
        owner=p.owner,
        repo=p.repo,
        url=URL_TEMPLATE.format(owner=p.owner, repo=p.repo),
        account=account,
        visibility=visibility,
    )
    _preview_create(p, visibility)
    if not yes and not confirm(
        f'Create {escape(p.owner)}/{escape(p.repo)} as a {visibility} repository?'
    ):
        raise PubanonError('repository creation declined')
    try:
        create_repository(dest, p.settings)
    except RepositoryCreationUncertainError as exc:
        raise PubanonError(
            'repository creation outcome is unknown; the repository may already exist',
            fact=exc.render(),
            recovery='pubanon repo verify',
        ) from exc

    try:
        verify_destination(paths, g, p)
        remote = ls_remote(dest, paths, p.name)
    except PubanonError as exc:
        raise PubanonError(
            'repository creation is partial; post-create verification failed',
            fact=exc.render(),
            recovery='pubanon repo verify',
        ) from exc
    if remote != RemoteRefs(None, {}, ()):
        raise PubanonError(
            'repository creation is partial; created repository is not empty',
            recovery='pubanon repo verify',
        )
    output(f'Created and verified an empty {visibility} host repository.', kind='success')
    output('The source repository and local publication state were not changed.')
    output()
    if candidate is None:
        next_step('pubanon prepare')
    elif candidate.state == STATE_UNKNOWN_OUTCOME:
        output('The pending publication outcome needs resolution.')
        next_step('pubanon publish --resolve')
    else:
        next_step('pubanon publish')


def settings_mismatches(expected: Settings, actual: SettingsFacts) -> list[str]:
    """Compare every declared setting with observed repository facts.

    Args:
        expected: Settings declared in the project configuration.
        actual: Settings observed from the host repository.

    Returns:
        One ordered diagnostic for each mismatched setting.
    """
    fields = (
        ('description', expected.description, actual.description),
        ('default_branch', expected.default_branch, actual.default_branch),
        ('issues', expected.issues, actual.issues),
        ('wiki', expected.wiki, actual.wiki),
        ('projects', expected.projects, actual.projects),
    )
    return [
        f'{name}: expected {_setting_value(wanted)}; actual {_setting_value(observed)}'
        for name, wanted, observed in fields
        if wanted != observed
    ]


def _verify_after_flip(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> str:
    """Verify the public repository and return its published commit."""
    recovery = 'fix the reported drift, then rerun pubanon repo reveal'

    def _partial_failure(
        *,
        fact: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
    ) -> PubanonError:
        return PubanonError(
            'the repository is now public but post-flip verification failed',
            fact=fact,
            expected=expected,
            actual=actual,
            recovery=recovery,
        )

    if p.settings is None:
        raise _partial_failure(fact='repository settings are missing')

    try:
        owner, repo, visibility = repository_facts(p.owner, p.repo)
        _require_configured_coordinates(p, owner, repo)
    except PubanonError as exc:
        raise _partial_failure(fact=exc.render()) from exc
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
        url=URL_TEMPLATE.format(owner=owner, repo=repo),
        account=g.accounts[p.host],
        visibility=visibility,
    )
    try:
        local_main, local_tags = published_refs(paths, p.name)
    except PubanonError as exc:
        raise _partial_failure(fact=exc.render()) from exc
    expected_refs = RemoteRefs(local_main, local_tags, ())
    try:
        remote = ls_remote(destination, paths, p.name)
    except PubanonError as exc:
        raise _partial_failure(fact=exc.render()) from exc
    if remote != expected_refs:
        raise _partial_failure(
            fact='published refs changed after the visibility flip',
            expected=escape(remote_refs_text(expected_refs)),
            actual=escape(remote_refs_text(remote)),
        )

    try:
        actual_settings = repository_settings(p.owner, p.repo)
    except PubanonError as exc:
        raise _partial_failure(fact=exc.render()) from exc
    mismatches = settings_mismatches(p.settings, actual_settings)
    if mismatches:
        raise _partial_failure(fact='; '.join(escape(item) for item in mismatches))
    return local_main or 'absent'


def _converge_public(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    host_changed: bool,
) -> None:
    """Verify and persist one completed public-state transition."""
    commit = _verify_after_flip(paths, g, p)
    try:
        save_project(paths, replace(p, visibility='public'))
    except PubanonError as exc:
        raise PubanonError(
            'the repository is public and verified but configuration update failed',
            fact=exc.render(),
            recovery='fix the configuration write, then rerun pubanon repo reveal',
        ) from exc
    output('Repository is public and verified.', kind='success')
    if host_changed:
        output('Host visibility changed to public, and local configuration now records it.')
    else:
        output('The host was already public; local configuration now records public visibility.')
    output('prior Actions history and logs are now public', kind='warning')
    output()
    output(f'{style("Details:")} {escape(f"verified commit {commit}")}')


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
        repository_settings(p.owner, p.repo),
    )
    if mismatches:
        raise PubanonError(
            'repository settings do not match configuration',
            fact='; '.join(escape(item) for item in mismatches),
        )
    output('repository settings match configuration', kind='success')


def _verify_before_flip(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    dest: Destination,
    *,
    already_public: bool = False,
) -> str:
    """Run the complete ordered verification before recording public visibility."""
    commit, tags = _verify_refs(paths, p, dest)
    _verify_checks(p, commit)
    _verify_package_observation(paths, p, dest)
    _verify_settings(paths, g, p)
    _verify_history(paths, g, p, (commit, tags))
    _verify_disclosure(paths, p, already_public=already_public)
    return commit


def reveal(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> None:
    """Reveal or reverify the configured repository from durable facts.

    Before recording a private-to-public transition, verify published refs,
    checks, optional package state, settings, history, and an interactively
    acknowledged disclosure inventory. Persist public visibility after
    verification. If host and configuration are already public, recheck refs
    and settings without repeating the full pre-reveal audit.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived Pubanon configuration and data paths.
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

    account = require_authenticated_account(g.accounts[p.host], authority='configuration')
    owner, repo, host_visibility = repository_facts(p.owner, p.repo)
    _require_configured_coordinates(p, owner, repo)

    if host_visibility == 'private' and p.visibility == 'private':
        destination = verify_destination(paths, g, p)
        _verify_before_flip(paths, g, p, destination)
        heading('Repository visibility change')
        output(f'Change {escape(owner)}/{escape(repo)} from private to public.', kind='warning')
        if not confirm('Make it public?'):
            raise PubanonError('repository reveal declined')
        _set_public_visibility(p)
        _converge_public(paths, g, p, host_changed=True)
        return

    if host_visibility == 'public' and p.visibility == 'private':
        output('host repository is already public; verifying before recording', kind='warning')
        output('repository content is already exposed; no rollback is assumed', kind='warning')
        destination = Destination(
            adapter=p.host,
            owner=owner,
            repo=repo,
            url=URL_TEMPLATE.format(owner=owner, repo=repo),
            account=account,
            visibility=host_visibility,
        )
        try:
            _verify_before_flip(paths, g, p, destination, already_public=True)
        except PubanonError as exc:
            raise PubanonError(
                'the repository is already public but verification failed',
                fact=exc.render(),
                recovery='fix the reported issue, then rerun pubanon repo reveal',
            ) from exc
        _converge_public(paths, g, p, host_changed=False)
        return

    if host_visibility == 'public' and p.visibility == 'public':
        commit = _verify_after_flip(paths, g, p)
        output('Repository is already public.', kind='success')
        output('Verified now: public visibility, published refs, and repository settings.')
        output()
        output(f'{style("Details:")} {escape(f"verified commit {commit}")}')
        output('Apply current privacy rules with a remote audit.')
        next_step('pubanon status --audit --remote')
        return

    if host_visibility == 'private' and p.visibility == 'public':
        project_file = paths.config_dir / 'projects' / f'{p.name}.toml'
        raise PubanonError(
            'declared visibility is public but the host repository is private',
            recovery=(
                'decide the intended visibility; make the host public outside Pubanon or set '
                f'visibility = "private" in {escape(str(project_file))}, then rerun '
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
