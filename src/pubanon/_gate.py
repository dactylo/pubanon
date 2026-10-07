"""Match configured content terms."""

import enum
import unicodedata
from collections.abc import Iterable, Mapping
from typing import NamedTuple

_SPAN_REGION_SIZE = 4096


class RuleClass(enum.Enum):
    """Classify content rules by publication impact.

    Both classes block matching candidate content and the pre-reveal history
    audit.

    Attributes:
        IDENTITY_CRITICAL: Private terms whose historical exposure requires
            explicit acknowledgment before publication or synchronization.
        HYGIENE: Terms reported as historical policy mismatches.
    """

    IDENTITY_CRITICAL = enum.auto()
    HYGIENE = enum.auto()


class GateHit(NamedTuple):
    """Identify one active rule term found in a named gate unit.

    Attributes:
        unit: Named content unit containing the match.
        rule_class: Publication-impact class of the matched rule.
        term: Active rule term found in the unit.
    """

    unit: str
    rule_class: RuleClass
    term: str


def _excerpt_span(data: bytes, start: int, end: int) -> tuple[int, int] | None:
    """Map exact matched bytes to display characters without splitting UTF-8."""
    if end > len(data):
        return None
    try:
        matched = data[start:end].decode('utf-8')
    except UnicodeDecodeError:
        return None
    prefix = len(data[:start].decode('utf-8', errors='replace'))
    return prefix, prefix + len(matched)


def _fold_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Return normalized casefolded text mapped to complete source spans."""
    decomposed: list[tuple[str, int]] = []
    segment_start = 0
    for source_index, source_character in enumerate(text):
        for character in unicodedata.normalize('NFD', source_character):
            combining_class = unicodedata.combining(character)
            if combining_class == 0:
                decomposed.append((character, source_index))
                segment_start = len(decomposed)
                continue
            insertion = len(decomposed)
            while (
                insertion > segment_start
                and unicodedata.combining(decomposed[insertion - 1][0]) > combining_class
            ):
                insertion -= 1
            decomposed.insert(insertion, (character, source_index))

    decomposed_spans: list[tuple[int, int]] = []
    segment_start = 0
    for index in range(1, len(decomposed) + 1):
        if index < len(decomposed) and unicodedata.combining(decomposed[index][0]) != 0:
            continue
        source_indexes = [
            source_index for _character, source_index in decomposed[segment_start:index]
        ]
        span = (min(source_indexes), max(source_indexes) + 1)
        decomposed_spans.extend([span] * (index - segment_start))
        segment_start = index

    folded: list[str] = []
    spans: list[tuple[int, int]] = []
    decomposed_index = 0
    for character in unicodedata.normalize('NFC', text):
        character_decomposition = unicodedata.normalize('NFD', character)
        contributor_spans = decomposed_spans[
            decomposed_index : decomposed_index + len(character_decomposition)
        ]
        decomposed_index += len(character_decomposition)
        span = (
            min(start for start, _end in contributor_spans),
            max(end for _start, end in contributor_spans),
        )
        for folded_character in character.casefold():
            folded.append(folded_character)
            spans.append(span)
    return ''.join(folded), spans


def _match_source_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Map one normalized match using only its overlapping source regions."""
    source_start = 0
    folded_start = 0
    matched_source_start: int | None = None
    matched_folded_start = 0
    while source_start < len(text):
        source_end = min(source_start + _SPAN_REGION_SIZE, len(text))
        # ASCII characters cannot be the second member of an NFC composition.
        # Splitting before one preserves both normalization and casefolding.
        # A long uninterrupted non-ASCII region retains the full-map fallback.
        while source_end < len(text) and not text[source_end].isascii():
            source_end += 1
        folded_end = folded_start + len(
            unicodedata.normalize('NFC', text[source_start:source_end]).casefold()
        )
        if folded_end > start and matched_source_start is None:
            matched_source_start = source_start
            matched_folded_start = folded_start
        if folded_end >= end and matched_source_start is not None:
            _folded, spans = _fold_with_spans(text[matched_source_start:source_end])
            matched = spans[start - matched_folded_start : end - matched_folded_start]
            return (
                matched_source_start + min(start for start, _end in matched),
                matched_source_start + max(end for _start, end in matched),
            )
        source_start = source_end
        folded_start = folded_end
    raise ValueError('normalized match lies outside its source')


def _term_hits(views: list[str], terms: Iterable[str]) -> list[str]:
    """Return configured terms found in either normalized content view."""
    folded = [(term, unicodedata.normalize('NFC', term).casefold()) for term in terms]
    return [term for term, needle in folded if any(needle in view for view in views)]


def _views(data: bytes) -> list[str]:
    """Return normalized caseless text views of byte content."""
    views = [
        unicodedata.normalize('NFC', data.decode('utf-8', errors='surrogateescape')).casefold()
    ]
    views.append(unicodedata.normalize('NFC', data.decode('latin-1')).casefold())
    return views


def first_match(data: bytes, term: str) -> tuple[int, int] | None:
    """Locate the first matching raw byte span in the existing gate views.

    Returns:
        Zero-based, end-exclusive offsets covering all source contributors.
        The lowest byte offset wins; UTF-8 with surrogate escapes takes
        precedence over Latin-1 on ties. None means neither view matches.
    """
    needle = unicodedata.normalize('NFC', term).casefold()
    matches: list[tuple[int, int]] = []
    for encoding in ('utf-8', 'latin-1'):
        text = data.decode(encoding, errors='surrogateescape')
        folded = unicodedata.normalize('NFC', text).casefold()
        index = folded.find(needle)
        if index < 0:
            continue
        if not needle:
            return (0, 0)
        start, end = _match_source_span(text, index, index + len(needle))
        matches.append(
            (
                len(text[:start].encode(encoding, errors='surrogateescape')),
                len(text[:end].encode(encoding, errors='surrogateescape')),
            )
        )
    return min(matches, key=lambda span: span[0]) if matches else None


def gate_unit(
    name: str,
    data: bytes,
    rules: Mapping[RuleClass, Iterable[str]],
) -> list[GateHit]:
    """Label normalized caseless term hits in one named content unit.

    Args:
        name: Name of the content unit.
        data: Byte content inspected through UTF-8 with surrogate escapes and
            Latin-1 text views. Both views are NFC-normalized and casefolded.
        rules: Terms grouped by rule class. Each term is NFC-normalized and
            casefolded for matching.

    Returns:
        One hit per matching configured term entry, in rule and term order,
        regardless of the number of occurrences or matching text views.
    """
    if not rules:
        return []
    views = _views(data)
    return [
        GateHit(name, rule_class, term)
        for rule_class, terms in rules.items()
        for term in _term_hits(views, terms)
    ]


def match_excerpt(data: bytes, start: int, end: int) -> tuple[str, tuple[int, int] | None]:
    """Return unescaped source context and its exact character highlight span.

    Args:
        data: Gate unit containing the match; undecodable bytes use replacement
            characters.
        start: Inclusive matched byte offset.
        end: Exclusive matched byte offset.

    Returns:
        Text with at most 60 surrounding bytes on each side, bounded by LF
        lines, with ellipses for omitted context. The character span is None
        when the matched bytes cannot be represented exactly as UTF-8 text.
        Callers own binary detection and terminal escaping.
    """
    line_start = data.rfind(b'\n', 0, start) + 1
    line_end = data.find(b'\n', end)
    if line_end < 0:
        line_end = len(data)
    left = max(line_start, start - 60)
    right = min(line_end, end + 60)
    raw_excerpt = data[left:right].rstrip(b'\r')
    excerpt = raw_excerpt.decode('utf-8', errors='replace')
    span = _excerpt_span(raw_excerpt, start - left, end - left)
    if left > line_start:
        excerpt = '...' + excerpt
        if span is not None:
            span = span[0] + 3, span[1] + 3
    if right < line_end:
        excerpt += '...'
    return excerpt, span
