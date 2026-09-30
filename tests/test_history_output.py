from pathlib import Path

import pytest

from pubanon import _history_output
from pubanon._gate import RuleClass
from pubanon._snapshots import _audit
from pubanon._terminal import color_context
from tests._setup import configure


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
