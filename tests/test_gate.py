import pytest

from pubanon import _gate
from pubanon._gate import GateHit, RuleClass, _term_hits, _views, gate_unit


@pytest.mark.parametrize('padding', [0, 32768])
def test_first_match_limits_span_mapping_to_the_matching_region(
    monkeypatch: pytest.MonkeyPatch, padding: int
) -> None:
    original = _gate._fold_with_spans
    mapped_sizes: list[int] = []

    def _observe(text: str) -> tuple[str, list[tuple[int, int]]]:
        mapped_sizes.append(len(text))
        return original(text)

    monkeypatch.setattr(_gate, '_fold_with_spans', _observe)
    data = b'x' * padding + b'canaryx' + b'x' * 32768
    assert _gate.first_match(data, 'canaryx') == (padding, padding + 7)
    assert mapped_sizes
    assert max(mapped_sizes) <= 4096


@pytest.mark.parametrize(
    ('text', 'term', 'matched'),
    [
        ('a\u0327\u0301', '\u0327', 'a\u0327\u0301'),
        ('a\u0301\u0315\u0327', '\u0327', 'a\u0301\u0315\u0327'),
        ('a\u0315\u0327\u0301', '\u0327', 'a\u0315\u0327\u0301'),
        ('a\u0301\u0327 b\u0301\u0327', '\u0327', 'a\u0301\u0327'),
        ('a\u0327\u0301b', '\u00e1\u0327', 'a\u0327\u0301'),
        ('a\u0327\u0301b', '\u0327b', 'a\u0327\u0301b'),
        ('a\u0301\u0327b', '\u0327b', 'a\u0301\u0327b'),
        ('\u00dfx', 'sx', '\u00dfx'),
        ('a\u0130\u0327b', 'a\u0130\u0327b', 'a\u0130\u0327b'),
    ],
)
def test_first_match_preserves_normalized_source_contributors(
    text: str, term: str, matched: str
) -> None:
    data = ('prefix ' + text + ' suffix').encode()
    start = len(b'prefix ')
    assert _gate.first_match(data, term) == (start, start + len(matched.encode()))
    assert gate_unit('unit', data, {RuleClass.IDENTITY_CRITICAL: (term,)}) == [
        GateHit('unit', RuleClass.IDENTITY_CRITICAL, term)
    ]


@pytest.mark.parametrize(
    ('prefix', 'source', 'term'),
    [
        ('x' * 4093, 'cafe\u0301 and Stra\u00dfe', 'caf\u00e9 and strasse'),
        ('x' * 4095, 'a\u0301\u0327', '\u0327'),
        ('x' * 4095, '\u1100\u1161\u11a8', '\uac01'),
        ('x' * 4094, 'aa\u0130\u0327b', 'aa\u0130\u0327b'),
        ('é' * 8192, 'canaryx', 'CANARYX'),
    ],
    ids=['casefold', 'combining', 'hangul', 'dotted-i', 'multibyte'],
)
def test_first_match_preserves_unicode_spans_across_mapping_boundaries(
    prefix: str, source: str, term: str
) -> None:
    data = (prefix + source + ' suffix').encode()
    start = len(prefix.encode())
    assert _gate.first_match(data, term) == (start, start + len(source.encode()))


@pytest.mark.parametrize(
    ('data', 'term', 'expected'),
    [
        (b'xx CANARYX canaryx', 'canaryx', b'CANARYX'),
        ('xx cafe\u0301 end'.encode(), 'caf\u00e9', 'cafe\u0301'.encode()),
        ('xx Stra\u00dfe end'.encode(), 'strasse', 'Stra\u00dfe'.encode()),
        (
            '\U0001f680 Privat\u00e9Alias'.encode(),
            'privat\u00e9alias',
            'Privat\u00e9Alias'.encode(),
        ),
        (b'\xff CAF\xc9 end', 'caf\u00e9', b'CAF\xc9'),
        ('xx a\u0301\u0327 end'.encode(), '\u0327', 'a\u0301\u0327'.encode()),
        ('xx \u1100\u1161 end'.encode(), '\uac00', '\u1100\u1161'.encode()),
        (b'xx\xffCANARYX', 'canaryx', b'CANARYX'),
        (b'\xe9 xx \xc3\xa9', '\u00e9', b'\xe9'),
    ],
)
def test_first_match_returns_original_byte_span(data: bytes, term: str, expected: bytes) -> None:
    hits = gate_unit('unit', data, {RuleClass.IDENTITY_CRITICAL: (term,)})
    assert hits == [GateHit('unit', RuleClass.IDENTITY_CRITICAL, term)]
    span = _gate.first_match(data, term)
    assert span is not None
    start, end = span
    assert data[start:end] == expected
    assert start == data.index(expected)
    assert _gate.first_match(data, 'not-present') is None


def test_gate_unit_accepts_iterator_terms_in_rule_order() -> None:
    assert gate_unit(
        'unit',
        b'CANARYX and ORCHIDX',
        {RuleClass.IDENTITY_CRITICAL: iter(('orchidx', 'canaryx'))},
    ) == [
        GateHit('unit', RuleClass.IDENTITY_CRITICAL, 'orchidx'),
        GateHit('unit', RuleClass.IDENTITY_CRITICAL, 'canaryx'),
    ]


def test_gate_unit_computes_views_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bytes] = []
    original = _views

    def _counting(data: bytes) -> list[str]:
        calls.append(data)
        return original(data)

    monkeypatch.setattr(_gate, '_views', _counting)
    rules = {
        _gate.RuleClass.IDENTITY_CRITICAL: ('secret',),
        _gate.RuleClass.HYGIENE: ('marker',),
    }
    hits = _gate.gate_unit('unit', b'has secret and marker', rules)
    assert {hit.term for hit in hits} == {'secret', 'marker'}
    assert len(calls) == 1


@pytest.mark.parametrize(
    'data',
    [
        pytest.param('canaryx'.encode('utf-16-le'), id='utf-16-le'),
        pytest.param('canaryx'.encode('utf-16-be'), id='utf-16-be'),
        pytest.param('canaryx'.encode('utf-32-le'), id='utf-32-le'),
        pytest.param('canaryx'.encode('utf-32-be'), id='utf-32-be'),
    ],
)
def test_gate_unit_does_not_decode_utf16_or_utf32(data: bytes) -> None:
    assert gate_unit('unit', data, {RuleClass.IDENTITY_CRITICAL: ('canaryx',)}) == []
    assert _gate.first_match(data, 'canaryx') is None


def test_gate_unit_does_not_fold_compatibility_characters() -> None:
    data = '\uff21'.encode()
    assert gate_unit('unit', data, {RuleClass.IDENTITY_CRITICAL: ('A',)}) == []
    assert _gate.first_match(data, 'A') is None


@pytest.mark.parametrize(
    'data',
    [
        pytest.param(b'Privat\xff\xc3\xa9Alias', id='invalid-byte-inside-term'),
        pytest.param(b'Privat\xc3', id='incomplete-utf8-boundary'),
    ],
)
def test_gate_unit_does_not_invent_term_across_invalid_utf8(data: bytes) -> None:
    assert (
        gate_unit(
            'mixed.bin',
            data,
            {RuleClass.IDENTITY_CRITICAL: ('Privat\u00e9Alias',)},
        )
        == []
    )


def test_gate_unit_labels_hits_with_rule_class() -> None:
    rules = {
        RuleClass.IDENTITY_CRITICAL: ['canaryx'],
        RuleClass.HYGIENE: ['orchidx'],
    }

    assert gate_unit('README.txt', b'CANARYX and ORCHIDX', rules) == [
        GateHit('README.txt', RuleClass.IDENTITY_CRITICAL, 'canaryx'),
        GateHit('README.txt', RuleClass.HYGIENE, 'orchidx'),
    ]


@pytest.mark.parametrize('rule_class', list(RuleClass))
@pytest.mark.parametrize(
    'text',
    ['Privat\u00e9Alias', 'Private\u0301Alias', 'PRIVAT\u00c9ALIAS'],
    ids=('composed', 'decomposed', 'casefolded'),
)
@pytest.mark.parametrize(
    'invalid_prefix',
    [pytest.param(True, id='invalid-prefix'), pytest.param(False, id='invalid-suffix')],
)
def test_gate_unit_preserves_intact_utf8_term_in_mixed_bytes(
    rule_class: RuleClass,
    text: str,
    invalid_prefix: bool,
) -> None:
    term = 'Privat\u00e9Alias'
    encoded = text.encode('utf-8')
    data = b'\xff' + encoded if invalid_prefix else encoded + b'\xff'

    assert gate_unit('mixed.bin', data, {rule_class: (term,)}) == [
        GateHit('mixed.bin', rule_class, term)
    ]


def test_term_hits_matches_mixed_case_term_after_invalid_byte() -> None:
    assert _term_hits(_views(b'\xffCaNaRyX'), ['canaryx']) == ['canaryx']


def test_term_hits_matches_nfc_term_in_nfd_content() -> None:
    assert _term_hits(_views('cafe\u0301'.encode()), ['caf\u00e9']) == ['caf\u00e9']


def test_term_hits_matches_uppercase_term_in_utf8() -> None:
    assert _term_hits(_views(b'say CANARYX'), ['canaryx']) == ['canaryx']


def test_term_hits_returns_empty_list_for_nonmatching_content() -> None:
    assert _term_hits(_views(b'quiet content'), ['canaryx']) == []
