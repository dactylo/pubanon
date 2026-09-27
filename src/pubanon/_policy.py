"""Evaluate configured publication path policy."""

import fnmatch
import glob
import unicodedata
from collections.abc import Collection, Container, Iterable
from typing import NamedTuple

RESERVED_DENY: frozenset[str] = frozenset({'.pubanon.toml'})
_SECRET_FILE_PATTERNS: tuple[str, ...] = (
    '.env',
    '.env.*',
    '*.pem',
    '*.key',
    '*_rsa',
    '*_ed25519',
    '*.p12',
    '*.pfx',
    '*.keystore',
    'credentials*.json',
    '.netrc',
    '.npmrc',
    '.pypirc',
    '.gitmodules',
)


class Membership(NamedTuple):
    """Partition paths by their policy membership.

    Attributes:
        allowed: Paths admitted by the configured allow patterns.
        denied: Paths excluded by the configured deny patterns.
        unmatched: Paths matched by neither configured pattern set.
        reserved: Paths refused because their names are reserved.
    """

    allowed: list[str]
    denied: list[str]
    unmatched: list[str]
    reserved: list[str]


def _is_secret_like(path: str) -> bool:
    """Return whether a path matches a credential-like convention."""
    candidates = (path, *path.split('/'))
    return any(
        fnmatch.fnmatchcase(candidate.casefold(), pattern)
        for candidate in candidates
        for pattern in _SECRET_FILE_PATTERNS
    )


def is_reserved_path(path: str) -> bool:
    """Return whether a posix path has an NFC/casefold-equivalent reserved basename."""
    basename = path.rsplit('/', maxsplit=1)[-1]
    return unicodedata.normalize('NFC', basename).casefold() in RESERVED_DENY


def evaluate_membership(
    paths: Iterable[str],
    allow: Collection[str],
    deny: Collection[str],
    *,
    global_deny: Collection[str] = (),
) -> Membership:
    """Partition paths by the configured publication policy.

    Reserved basenames take precedence over deny patterns, which take
    precedence over allow patterns. Patterns use case-sensitive `fnmatch`
    matching against the entire path; slashes are ordinary characters.
    Global exclusions match case-insensitively and override project rules.

    Args:
        paths: Posix paths to classify.
        allow: Operator allow patterns.
        deny: Operator deny patterns.
        global_deny: Global exclusion patterns, matched using Unicode case
            folding.

    Returns:
        Paths partitioned by policy outcome, preserving input order within each
        partition.
    """
    allowed: list[str] = []
    denied: list[str] = []
    unmatched: list[str] = []
    reserved: list[str] = []
    folded_global_deny = tuple(pattern.casefold() for pattern in global_deny)
    for path in paths:
        if is_reserved_path(path):
            reserved.append(path)
        elif any(
            fnmatch.fnmatchcase(path.casefold(), pattern) for pattern in folded_global_deny
        ) or any(fnmatch.fnmatchcase(path, pattern) for pattern in deny):
            denied.append(path)
        elif any(fnmatch.fnmatchcase(path, pattern) for pattern in allow):
            allowed.append(path)
        else:
            unmatched.append(path)
    return Membership(allowed, denied, unmatched, reserved)


def literal_pattern(path: str) -> str:
    """Convert one literal Git path to an operator glob pattern.

    Args:
        path: Literal posix path from a Git tree.

    Returns:
        An `fnmatch` pattern that matches the literal path itself.
    """
    return glob.escape(path)


def narrowest_offer(
    unmatched: Iterable[str],
    *,
    guided: bool = False,
) -> list[tuple[str, list[str]]]:
    """Offer literal paths or patterns for shared top-level directories.

    Args:
        unmatched: Paths outside the configured membership policy.
        guided: Whether to expose nested documentation and agent configuration
            groups used by initial project review.

    Returns:
        Patterns paired with their supplied unmatched paths in first-seen
        order. Ordinary grouping uses top-level directories. Guided grouping
        separates documentation subdirectories and agent configuration.
        Directory patterns can also admit paths outside the supplied input.
    """
    paths = list(unmatched)
    nested_docs = guided and any(
        path.startswith('docs/') and path.count('/') > 1 for path in paths
    )
    groups: dict[str, list[str]] = {}
    directory_groups: set[str] = set()
    for path in paths:
        if guided and path.startswith('.agents/'):
            group = '.agents'
            directory_groups.add(group)
        elif nested_docs and path.startswith('docs/'):
            group = '/'.join(path.split('/')[:2])
            if group != path:
                directory_groups.add(group)
        else:
            group = path.split('/', maxsplit=1)[0]
        groups.setdefault(group, []).append(path)

    offers: list[tuple[str, list[str]]] = []
    for group, grouped_paths in groups.items():
        pattern = (
            f'{literal_pattern(group)}/**'
            if group in directory_groups
            else (
                literal_pattern(grouped_paths[0])
                if len(grouped_paths) == 1
                else f'{literal_pattern(group)}/**'
            )
        )
        offers.append((pattern, grouped_paths))
    return offers


def secret_like(paths: Iterable[str], exceptions: Container[str]) -> list[str]:
    """Return credential-like paths lacking exact recorded exceptions.

    Args:
        paths: Posix paths to inspect.
        exceptions: Exactly authorized paths.

    Returns:
        Credential-like paths without recorded exceptions.
    """
    return [path for path in paths if path not in exceptions and _is_secret_like(path)]
