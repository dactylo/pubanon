"""Find a version's section in a Markdown changelog."""

import re
import string
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

_FENCE = re.compile(r' {0,3}(`{3,}|~{3,})')
_HEADING = re.compile(r'(#{1,6})(?:[ \t]|$)')
_NEAR_MISS_LIMIT = 3
_SPACES = re.compile(r'[ \t]+')
_STRICT = r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'
_STRICT_MENTION = re.compile(r'(?<![\w.+-])v?(' + _STRICT + r')(?![\w.+-])')
_VERSION_END = r'(?![\w.+-])'
_VERSION_LIKE = r'[0-9]+(?:\.[0-9]+)+(?:(?:[-+.]|[^\W\d_])[\w.+-]*)?'
_WORD = re.compile(r'[\w.+-]+')

type Reason = Literal['no heading', 'several headings', 'empty section', 'not UTF-8']


@dataclass(frozen=True)
class NearMiss:
    """Locate a heading that mentions a version but does not match the heading rule.

    Attributes:
        line: One-based line number in the changelog.
        text: The heading line as written.
    """

    line: int
    text: str


@dataclass(frozen=True)
class Lookup:
    """Report a version's changelog section, or why there is none.

    Attributes:
        section: Section text without its heading or surrounding blank lines,
            or None when there is no section.
        reason: Why there is no section; None when `section` is set.
        lines: One-based line numbers of every matching heading when several
            match.
        near_misses: Up to three headings that mention the version without
            matching, in file order, when no heading matches.
        suggestion: A `changelog_heading` template matching the first near miss
            with no digit before the version, not yet validated.
    """

    section: str | None
    reason: Reason | None = None
    lines: tuple[int, ...] = ()
    near_misses: tuple[NearMiss, ...] = ()
    suggestion: str | None = None


@dataclass(frozen=True)
class Recognition:
    """Report the versions that a changelog's headings name, for setup.

    Attributes:
        versions: Distinct strict `X.Y.Z` versions with matching headings,
            newest first.
        readable: False when the changelog is not UTF-8.
        near_misses: Up to three headings that name a strict version without
            matching, in file order, when no heading matches.
        suggestion: A `changelog_heading` template matching the first near miss
            with no digit before its version, not yet validated.
    """

    versions: tuple[str, ...]
    readable: bool = True
    near_misses: tuple[NearMiss, ...] = ()
    suggestion: str | None = None


def _headings(lines: Sequence[str]) -> list[tuple[int, int]]:
    """Return the index and level of every heading line outside fenced code blocks."""
    headings: list[tuple[int, int]] = []
    fence = ''
    for index, line in enumerate(lines):
        marker = _FENCE.match(line)
        if fence:
            if (
                marker is not None
                and marker.group(1)[0] == fence[0]
                and len(marker.group(1)) >= len(fence)
                and not line[marker.end() :].strip(' \t')
            ):
                fence = ''
            continue
        if marker is not None and not (marker.group(1)[0] == '`' and '`' in line[marker.end() :]):
            fence = marker.group(1)
            continue
        heading = _HEADING.match(line)
        if heading is not None:
            headings.append((index, len(heading.group(1))))
    return headings


def _is_version_character(character: str) -> bool:
    """Report whether a character can continue a version."""
    return _WORD.fullmatch(character) is not None


def _lines(data: bytes) -> list[str] | None:
    """Split UTF-8 changelog bytes into lines, or return None for other bytes."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return None
    return text.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n').split('\n')


def _literal_pattern(text: str) -> str:
    """Match literal template text, letting each run of spaces match spaces or tabs."""
    return '[ \t]+'.join(re.escape(part) for part in _SPACES.split(text))


def _mention(line: str, version: str) -> int | None:
    """Return where a line names a version as a whole word, allowing one `v` before it."""
    start = 0
    while (index := line.find(version, start)) >= 0:
        before = line[:index].removesuffix('v')
        after = line[index + len(version) :]
        if not (before and _is_version_character(before[-1])) and not (
            after and _is_version_character(after[0])
        ):
            return index
        start = index + 1
    return None


def _pattern(template: str | None, word: str) -> re.Pattern[str]:
    """Compile the heading rule with `word`, a pattern without groups, as its version."""
    if template is None:
        return re.compile(r'#{1,2}[ \t]+\[?v?(' + word + ')' + _VERSION_END)
    before: list[str] = []
    after: list[str] = []
    target = before
    for literal, field, _spec, _conversion in string.Formatter().parse(template):
        target.append(literal)
        if field is not None:
            target = after
    suffix = _literal_pattern(''.join(after)) or _VERSION_END
    return re.compile(_literal_pattern(''.join(before)) + '(' + word + ')' + suffix)


def _suggestion(mentions: Sequence[tuple[str, int]]) -> str | None:
    """Suggest a template from the first mention with no digit before its version."""
    for text, index in mentions:
        before = text[:index]
        if not any(character.isdigit() for character in before):
            return before.replace('{', '{{').replace('}', '}}') + '{version}'
    return None


def find_section(data: bytes, version: str, template: str | None = None) -> Lookup:
    """Find the changelog section for one version.

    Args:
        data: Changelog bytes from the published snapshot.
        version: Strict `X.Y.Z` version, without a leading `v`.
        template: Validated `changelog_heading`, or None for the built-in rule.

    Returns:
        The section, or the reason there is none with any hints.
    """
    lines = _lines(data)
    if lines is None:
        return Lookup(None, 'not UTF-8')
    exact = _pattern(template, re.escape(version))
    headings = _headings(lines)
    matching = [
        (index, level) for index, level in headings if exact.match(lines[index]) is not None
    ]
    if not matching:
        mentions = [
            (index, found)
            for index, _level in headings
            if (found := _mention(lines[index], version)) is not None
        ][:_NEAR_MISS_LIMIT]
        return Lookup(
            None,
            'no heading',
            near_misses=tuple(NearMiss(index + 1, lines[index]) for index, _found in mentions),
            suggestion=_suggestion([(lines[index], found) for index, found in mentions]),
        )
    if len(matching) > 1:
        return Lookup(None, 'several headings', lines=tuple(index + 1 for index, _ in matching))
    start, level = matching[0]
    versioned = _pattern(template, _VERSION_LIKE)
    end = len(lines)
    for index, other in headings:
        if index > start and (other <= level or versioned.match(lines[index]) is not None):
            end = index
            break
    body = lines[start + 1 : end]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return Lookup(None, 'empty section')
    return Lookup('\n'.join(body))


def recognize(data: bytes, template: str | None = None) -> Recognition:
    """List the strict versions that a changelog's headings name.

    Args:
        data: Changelog bytes from the source.
        template: Validated `changelog_heading`, or None for the built-in rule.

    Returns:
        The recognized versions, newest first, or hints when there are none.
    """
    lines = _lines(data)
    if lines is None:
        return Recognition((), readable=False)
    strict = _pattern(template, _STRICT)
    headings = _headings(lines)
    versions = {
        found.group(1)
        for index, _level in headings
        if (found := strict.match(lines[index])) is not None
    }
    if versions:
        return Recognition(
            tuple(
                sorted(
                    versions,
                    key=lambda version: tuple(int(part) for part in version.split('.')),
                    reverse=True,
                )
            )
        )
    mentions = [
        (index, found.start(1))
        for index, _level in headings
        if (found := _STRICT_MENTION.search(lines[index])) is not None
    ][:_NEAR_MISS_LIMIT]
    return Recognition(
        (),
        near_misses=tuple(NearMiss(index + 1, lines[index]) for index, _start in mentions),
        suggestion=_suggestion([(lines[index], start) for index, start in mentions]),
    )
