import re
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import _history_output
from pubanon._gate import GateHit, RuleClass
from pubanon._snapshots import _audit
from pubanon._terminal import color_context, escape, verbose_context
from tests._setup import configure


def _summary_audit(*rows: tuple[str, RuleClass, str, str | None]) -> _audit.HistoryAudit:
    evidence = sorted(
        (
            _audit.HistoryEvidence(_audit.AuditFinding(unit, rule_class, term), commit, 'x', 'x')
            for unit, rule_class, term, commit in rows
        ),
        key=lambda item: (item.finding.unit, item.finding.rule_class.name, item.finding.term),
    )
    return _audit.HistoryAudit(tuple(item.finding for item in evidence), (), tuple(evidence))


def test_earliest_release_names_oldest_witness_by_tags_or_commit() -> None:
    older, newer = 'a' * 40, 'b' * 40
    critical = RuleClass.IDENTITY_CRITICAL
    result = _summary_audit(
        ('path:a.txt', critical, 'term', newer), ('path:b.txt', critical, 'term', older)
    )

    assert _history_output.earliest_release(result, [newer, older], {'v1.0.0': older}) == 'v1.0.0'
    assert _history_output.earliest_release(result, [newer, older], {}) == older[:12]
    assert (
        _history_output.earliest_release(
            _summary_audit(('tag:term', critical, 'term', None)), [newer, older], {}
        )
        == 'the earliest affected release'
    )


def test_groups_findings_by_snapshot_with_release_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _paths, global_config, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    older, newer = 'a' * 40, 'b' * 40
    evidence = tuple(
        _audit.HistoryEvidence(
            _audit.AuditFinding(unit, RuleClass.IDENTITY_CRITICAL, 'Private Person'),
            commit,
            'blob bytes 0:14',
            label,
            'Private Person',
        )
        for unit, commit, label in (
            ('path:a.txt', older, 'a.txt:1'),
            ('path:b.txt', newer, 'b.txt:1'),
            ('path:c.txt', older, 'c.txt:1'),
            ('tag:unresolved', None, 'tag name'),
        )
    )
    result = _audit.HistoryAudit(tuple(item.finding for item in evidence), (), evidence)

    with color_context('never'):
        output = '\n'.join(
            _history_output.finding_groups(
                result, (global_config, project), (), [newer, older], {'v1.0.0': older}
            )
        )

    newest_heading = f'Published snapshot: {newer[:12]}'
    older_heading = f'Published snapshot: v1.0.0 ({older[:12]})'
    unknown_heading = 'Published snapshot: unknown'
    assert output.count('Warning: historical exposure') == 1
    for heading in (newest_heading, older_heading, unknown_heading):
        assert output.count(heading) == 1
    newest, older_group, unknown = (
        output.index(heading) for heading in (newest_heading, older_heading, unknown_heading)
    )
    assert newest < output.index('  b.txt:1') < older_group
    assert older_group < output.index('  a.txt:1') < output.index('  c.txt:1') < unknown
    assert unknown < output.index('  tag unresolved')


@pytest.mark.parametrize(
    ('text', 'term', 'highlighted'),
    [
        ('before SECRET after', 'secret', 'before \x1b[33mSECRET\x1b[0m after'),
        ('before cafe\u0301 after', 'caf\u00e9', 'before \x1b[33mcafe\u0301\x1b[0m after'),
        (
            'x' * 80 + '...target',
            '...',
            '...' + 'x' * 60 + '\x1b[33m...\x1b[0mtarget',
        ),
        (
            '\x1b[31msecret\\suffix',
            'secret',
            r'\x1b[31m' + '\x1b[33msecret\x1b[0m' + r'\\suffix',
        ),
        ('\u00e9', '\u00c3', None),
    ],
)
def test_highlights_source_match_without_changing_excerpt(
    text: str, term: str, highlighted: str | None
) -> None:
    findings: dict[_audit.AuditFinding, _audit.HistoryEvidence] = {}
    _audit._add_findings(
        findings,
        'path:notes.txt',
        text.encode(),
        {RuleClass.IDENTITY_CRITICAL: (term,)},
        ('a' * 40, 'blob'),
    )

    with color_context('always'):
        lines = _history_output._finding_lines(
            next(iter(findings.values())), ('global sensitive_terms entry 1',)
        )

    assert lines[1] == '    Rule: global sensitive_terms, item 1'
    if highlighted is None:
        assert lines[2] == f'    Matched: \x1b[33m{term}\x1b[0m'
        assert lines[3] == f'    Text: {text}'
    else:
        assert lines[2] == f'    Text: {highlighted}'


def test_history_reminder_counts_distinct_terms_and_locations_across_classes() -> None:
    older = 'a' * 40
    result = _summary_audit(
        ('path:a.txt', RuleClass.IDENTITY_CRITICAL, 'x', older),
        ('path:a.txt', RuleClass.HYGIENE, 'y', older),
        ('path:b.txt', RuleClass.IDENTITY_CRITICAL, 'x', older),
    )

    rendered = _history_output.history_reminder(result.findings)
    assert re.search(r'\b2 flagged terms in 2 locations\b', rendered)
    assert 'pubanon repo reveal' in rendered
    assert 'before making them public' in rendered
    assert 'pubanon audit' in rendered
    assert re.search(
        r'\b1 flagged term in 1 location\b',
        _history_output.history_reminder(result.findings[:1]),
    )


def test_keeps_full_tag_name_when_source_excerpt_is_shortened() -> None:
    name = 'v1.0.0-' + 'x' * 100 + '-blocked'
    findings: dict[_audit.AuditFinding, _audit.HistoryEvidence] = {}
    _audit._add_findings(
        findings,
        f'tag:{name}',
        name.encode(),
        {RuleClass.IDENTITY_CRITICAL: ('blocked',)},
        (None, 'tag name'),
    )

    with color_context('never'):
        lines = _history_output._finding_lines(
            next(iter(findings.values())), ('global sensitive_terms entry 1',)
        )

    assert lines[0] == f'  tag {name}'
    assert lines[1] == '    Rule: global sensitive_terms, item 1'
    assert lines[2] == '    Matched: blocked'
    assert lines[3].startswith('    Text: ...')


def test_rules_line_accepts_gate_hits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _paths, global_config, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    global_config = replace(global_config, sensitive_terms=('term-a', 'term-b'))
    hits = [
        GateHit('job 1 log', RuleClass.IDENTITY_CRITICAL, 'term-b'),
        GateHit('run 1 details', RuleClass.IDENTITY_CRITICAL, 'term-a'),
    ]

    assert (
        _history_output.rules_line(hits, (global_config, project), ())
        == 'Rules: global sensitive_terms entries 1, 2'
    )


def test_summary_block_shows_row_details_and_limits_rows_unless_unlimited() -> None:
    rows = [
        (f'job {index:02}', ['term-a'], (f'https://example.invalid/{index}',))
        for index in range(21)
    ]

    with color_context('never'):
        limited = _history_output.summary_block('Findings', (1, 21), [('Run 1:', rows)])
        unlimited = _history_output.summary_block(
            'Findings', (1, 21), [('Run 1:', rows)], unlimited=True
        )

    assert limited[:4] == [
        'Findings: 1 term in 21 locations',
        '  Run 1:',
        '    job 00  term-a',
        '      https://example.invalid/0',
    ]
    assert limited[-1] == '  and 1 more row'
    assert unlimited[-2:] == ['    job 20  term-a', '      https://example.invalid/20']
    assert not any(line.startswith('  and ') for line in unlimited)


def test_summary_escapes_labels_and_leaves_long_labels_unpadded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _paths, global_config, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    global_config = replace(global_config, sensitive_terms=('term-\x07a',))
    older = 'a' * 40
    long_path = 'docs/' + 'x' * 40 + '.md'
    result = _summary_audit(
        ('path:a\x1b[31m.txt', RuleClass.IDENTITY_CRITICAL, 'term-\x07a', older),
        (f'path:{long_path}', RuleClass.IDENTITY_CRITICAL, 'term-\x07a', older),
    )

    with color_context('never'), verbose_context(enabled=True):
        lines = _history_output.finding_summary(result, (global_config, project), (), [older], {})

    term = escape('term-\x07a')
    assert f'  First published in commit {older}:' in lines
    assert f'    {escape("a\x1b[31m.txt")}  {term}' in lines
    assert f'    {long_path}  {term}' in lines
    assert not any('\x1b' in line or '\x07' in line for line in lines)


def test_summary_groups_locations_by_first_published_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _paths, global_config, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    global_config = replace(global_config, sensitive_terms=('term-a', 'term-b'))
    older, newer = 'a' * 40, 'b' * 40
    critical = RuleClass.IDENTITY_CRITICAL
    result = _summary_audit(
        ('path:Makefile', critical, 'term-a', older),
        ('path:Makefile', critical, 'term-b', newer),
        ('path:docs/guide.md', critical, 'term-b', older),
        (f'{newer[:12]}:message', critical, 'term-a', newer),
        (f'{newer[:12]}:header:author', critical, 'term-a', newer),
        ('tag:term-a-tag', critical, 'term-a', None),
    )

    with color_context('never'):
        lines = _history_output.finding_summary(
            result, (global_config, project), (), [newer, older], {'v1.0.0': older}
        )

    assert lines == [
        'Sensitive terms in published history: 2 terms in 5 locations',
        f'  First published in v1.0.0 ({older[:12]}):',
        '    Makefile        term-a',
        '    docs/guide.md   term-b',
        f'  First published in commit {newer[:12]}:',
        '    commit author   term-a',
        '    commit message  term-a',
        '    Makefile        term-b',
        '  Not in published commits:',
        '    tag term-a-tag  term-a',
        '  Rules: global sensitive_terms entries 1, 2',
        '  pubanon audit shows each match in context.',
    ]


def test_summary_limits_rows_and_counts_hidden_rows_not_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _paths, global_config, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    global_config = replace(global_config, sensitive_terms=('term-a', 'term-b'))
    older, newer = 'a' * 40, 'b' * 40
    critical = RuleClass.IDENTITY_CRITICAL
    # 22 files first published in the older snapshot, and file00 again in the newer one:
    # 23 rows but only 22 locations.
    result = _summary_audit(
        *((f'path:file{index:02}.txt', critical, 'term-a', older) for index in range(22)),
        ('path:file00.txt', critical, 'term-b', newer),
    )

    with color_context('never'):
        lines = _history_output.finding_summary(
            result, (global_config, project), (), [newer, older], {}
        )

    assert lines[0] == 'Sensitive terms in published history: 2 terms in 22 locations'
    assert lines[1] == f'  First published in commit {older[:12]}:'
    assert lines[21] == '    file19.txt  term-a'
    assert lines[22] == '  and 3 more rows'
    assert lines[23] == '  Rules: global sensitive_terms entries 1, 2'
    assert len(lines) == 25
    assert f'  First published in commit {newer[:12]}:' not in lines


def test_summary_separates_rule_classes_and_names_single_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _paths, global_config, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    global_config = replace(global_config, sensitive_terms=('term-a',))
    project = replace(project, hygiene_terms=('style-term',))
    older = 'a' * 40
    result = _summary_audit(
        ('path:a.txt', RuleClass.IDENTITY_CRITICAL, 'term-a', older),
        ('path:b.txt', RuleClass.HYGIENE, 'style-term', older),
    )

    with color_context('never'):
        lines = _history_output.finding_summary(result, (global_config, project), (), [older], {})

    assert lines == [
        'Sensitive terms in published history: 1 term in 1 location',
        f'  First published in commit {older[:12]}:',
        '    a.txt  term-a',
        '  Rules: global sensitive_terms entry 1',
        'Policy terms in published history: 1 term in 1 location',
        f'  First published in commit {older[:12]}:',
        '    b.txt  style-term',
        '  Rules: project hygiene_terms entry 1',
        '  pubanon audit shows each match in context.',
    ]
