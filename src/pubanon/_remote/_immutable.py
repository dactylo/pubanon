"""Offer GitHub immutable releases once the final repository is public."""

from pubanon._config import ProjectConfig
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host
from pubanon._remote._host import Destination, GitHubRelease, ImmutableReleases
from pubanon._terminal import confirm, escape, output

_CANCELED = 'Canceled. The repository is public; immutable releases are unchanged.'
_EXPLANATION = (
    "  When on, GitHub keeps each release's tag and files fixed and signs a record of the "
    "release; titles and notes stay editable. A deleted release's version can never be used "
    'again, even in a recreated repository, and pubanon cannot replace history that moves '
    'these tags.'
)
_GUIDE = 'See "Immutable releases" in the guide.'


def _age(page: GitHubRelease) -> tuple[str, int]:
    """Order release pages from oldest to newest."""
    return page.created_at, page.identifier


def _content(page: GitHubRelease) -> tuple[object, ...]:
    """Return the page fields a conversion must leave unchanged."""
    return (
        page.tag,
        page.title,
        page.notes,
        page.draft,
        page.prerelease,
        page.created_at,
        page.published_at,
    )


def _convert(destination: Destination, page: GitHubRelease, sent: list[str]) -> None:
    """Convert one mutable page using its current title and verify unchanged content."""
    current = _host.read_release_by_id(destination, page.identifier)
    if current.immutable:
        return
    if not current.title:
        raise PubanonError(f'release page {escape(page.tag)} lost its title')
    _host.require_authenticated_account(destination.account, authority='configuration')
    sent.append(f'convert {escape(page.tag)}')
    _host.convert_release(destination, current.identifier, current.title)
    after = _host.read_release_by_id(destination, current.identifier)
    if not after.immutable or _content(after) != _content(current):
        raise PubanonError(
            f'release page {escape(page.tag)} did not read back immutable and unchanged'
        )


def _counted(count: int, noun: str) -> str:
    """Return a count with a regular plural noun."""
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def _enable(destination: Destination, sent: list[str]) -> None:
    """Enable immutable releases with the configured account and verify the setting."""
    _host.require_authenticated_account(destination.account, authority='configuration')
    sent.append('turn on immutable releases')
    _host.enable_immutable_releases(destination.owner, destination.repo)
    if not _host.immutable_releases(destination.owner, destination.repo).enabled:
        raise PubanonError('the setting did not read back as on')


def _note_untitled(tags: list[str]) -> None:
    """Name pages that stay mutable because they have no title."""
    if tags:
        output(f'Release pages without a title stay mutable: {", ".join(tags)}. {_GUIDE}')


def _prompt(setting: ImmutableReleases, count: int) -> tuple[str, str] | None:
    """Return the status line and question, or None when nothing applies."""
    pages = _counted(count, 'existing release page')
    if setting.enabled:
        if not count:
            return None
        verb = 'is' if count == 1 else 'are'
        return (
            f'Immutable releases: on; {_counted(count, "release page")} {verb} still mutable',
            f'Make the {pages} immutable?',
        )
    if setting.enforced_by_owner:
        return None
    question = (
        f'Turn on immutable releases, including the {pages}?'
        if count
        else 'Turn on immutable releases?'
    )
    return 'Immutable releases: off', question


def _report(destination: Destination, sent: list[str]) -> str:
    """Read the setting and pages again and describe what is now true."""
    try:
        setting = _host.immutable_releases(destination.owner, destination.repo)
        pages = sorted(
            (page for page in _host.all_releases(destination) if not page.draft), key=_age
        )
    except (PubanonError, KeyboardInterrupt) as exc:
        requests = '; '.join(sent) if sent else 'none'
        report = (
            f'pubanon could not read the result. Requests sent: {requests}. '
            'Their outcome is unknown.'
        )
        if isinstance(exc, KeyboardInterrupt):
            raise OperationCanceled('Canceled. The repository is public.', fact=report) from None
        return report
    lines = [f'Immutable releases: {"on" if setting.enabled else "off"}']
    immutable = [escape(page.tag) for page in pages if page.immutable]
    mutable = [escape(page.tag) for page in pages if not page.immutable]
    if immutable:
        lines.append('Immutable: ' + ', '.join(immutable))
    if mutable:
        lines.append('Still mutable: ' + ', '.join(mutable))
    return '\n'.join(lines)


def _apply(
    destination: Destination, setting: ImmutableReleases, pages: list[GitHubRelease]
) -> bool:
    """Turn the setting on and convert pages; warn and return False on failure."""
    sent: list[str] = []
    try:
        if not setting.enabled:
            _enable(destination, sent)
        for page in pages:
            _convert(destination, page, sent)
    except KeyboardInterrupt:
        raise OperationCanceled(
            'Canceled. The repository is public.', fact=_report(destination, sent)
        ) from None
    except PubanonError as exc:
        output(f'warning: immutable releases did not finish: {escape(str(exc))}', kind='warning')
        for line in _report(destination, sent).split('\n'):
            output(f'  {line}')
        output(f'  {_GUIDE}')
        return False
    return True


def offer(p: ProjectConfig, destination: Destination) -> None:
    """Ask once whether to turn on immutable releases and convert existing pages.

    Ask only when the project has release pages enabled or the repository has a
    page, and either the setting is off and not enforced by the owner or some
    non-draft page with a title is still mutable. Pages without a title stay
    mutable, because giving one a title would change it.

    Args:
        p: Project configuration of the revealed repository.
        destination: Verified final repository and its configured account.

    Raises:
        OperationCanceled: Ctrl-C at the question or during the changes; the
            message states what is known to have changed.
    """
    try:
        pages = _host.all_releases(destination)
        enabled = p.release_page is not None and p.release_page.enabled is True
        if not enabled and not pages:
            return
        setting = _host.immutable_releases(destination.owner, destination.repo)
    except KeyboardInterrupt:
        raise OperationCanceled(_CANCELED) from None
    except PubanonError:
        output(f'warning: could not read the immutable releases setting. {_GUIDE}', kind='warning')
        return
    mutable = sorted((page for page in pages if not page.draft and not page.immutable), key=_age)
    convertible = [page for page in mutable if page.title]
    untitled = [escape(page.tag) for page in mutable if not page.title]
    prompt = _prompt(setting, len(convertible))
    if prompt is None:
        _note_untitled(untitled)
        return
    status, question = prompt
    output(status)
    output(_EXPLANATION)
    try:
        accepted = confirm(question)
    except OperationCanceled:
        raise OperationCanceled(_CANCELED) from None
    except PubanonError:
        output(
            'warning: could not ask about immutable releases; the repository is public and '
            f'immutable releases are unchanged. {_GUIDE}',
            kind='warning',
        )
        return
    count = _counted(len(convertible), 'existing release page')
    if not accepted:
        if setting.enabled:
            verb = 'stays' if len(convertible) == 1 else 'stay'
            output(
                f'Immutable releases stay on; {count} {verb} mutable. The guide explains how '
                'to convert them.'
            )
        else:
            output('Immutable releases stay off. The guide explains how to turn them on later.')
    elif _apply(destination, setting, convertible):
        including = f', including {count}' if convertible else ''
        output(f'Immutable releases: on{including}.', kind='success')
    _note_untitled(untitled)
