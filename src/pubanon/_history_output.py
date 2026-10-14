"""Render historical findings in detail or as a compact summary."""

from collections.abc import Collection, Iterable, Mapping, Sequence

from pubanon._config import GlobalConfig, ProjectConfig, rule_labels
from pubanon._gate import GateHit, RuleClass
from pubanon._snapshots import _audit
from pubanon._terminal import color_enabled, escape, style, verbose_enabled

_EARLIEST_UNKNOWN = 'the earliest affected release'
HISTORY_LABELS = {
    RuleClass.IDENTITY_CRITICAL: 'historical exposure',
    RuleClass.HYGIENE: 'policy mismatch',
}
_RULE_SOURCES = (
    'global sensitive_terms',
    'project extra_terms',
    'derived private path',
    'built-in hygiene rule',
    'project hygiene_terms',
)
_SUMMARY_HEADLINES = {
    RuleClass.IDENTITY_CRITICAL: 'Sensitive terms in published history',
    RuleClass.HYGIENE: 'Policy terms in published history',
}
_SUMMARY_LABEL_WIDTH = 40
_SUMMARY_ROW_LIMIT = 20

type SummaryRow = tuple[str, Sequence[str], Sequence[str]]
type SummaryGroup = tuple[str, Sequence[SummaryRow]]


def _counted(count: int, noun: str) -> str:
    """Return a count with a regular plural noun."""
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def _finding_lines(evidence: _audit.HistoryEvidence, origins: tuple[str, ...]) -> list[str]:
    """Render one escaped finding with its location before its supporting evidence."""
    finding = evidence.finding
    location = evidence.label
    if finding.unit.startswith('path:') and (
        evidence.label == 'filename' or evidence.excerpt is None
    ):
        location = f'{finding.unit.removeprefix("path:")} ({location})'
    elif finding.unit.startswith('tag:'):
        location = f'tag {finding.unit.removeprefix("tag:")}'
    sources = '; '.join(label.replace(' entry ', ', item ') for label in origins)
    lines = [
        '  ' + style(escape(location), 'label'),
        f'    Rule: {escape(sources)}',
    ]
    excerpt = evidence.excerpt
    span = evidence.excerpt_span if color_enabled() else None
    if excerpt is None or span is None:
        lines.append('    Matched: ' + style(escape(finding.term), 'warning'))
    if excerpt is not None:
        if span is None:
            text = escape(excerpt)
        else:
            start, end = span
            text = (
                escape(excerpt[:start])
                + style(escape(excerpt[start:end]), 'warning')
                + escape(excerpt[end:])
            )
        lines.append(f'    Text: {text}')
    return lines


def _location_label(unit: str) -> str:
    """Name one audited unit as a summary location."""
    if unit.startswith('path:'):
        return unit.removeprefix('path:')
    if unit.startswith('tag:'):
        return f'tag {unit.removeprefix("tag:")}'
    _commit, _separator, kind = unit.partition(':')
    if kind == 'message':
        return 'commit message'
    if kind.startswith('header:'):
        return f'commit {kind.removeprefix("header:")}'
    return unit


def _snapshot_heading(commit: str | None, tags: Mapping[str, str]) -> str:
    """Label the witness snapshot with its published tags and commit ID."""
    identity = 'unknown'
    if commit is not None:
        identity = commit if verbose_enabled() else commit[:12]
        versions = ', '.join(name for name, target in tags.items() if target == commit)
        if versions:
            identity = f'{versions} ({identity})'
    return style(f'Published snapshot: {escape(identity)}', 'label')


def _summary_heading(commit: str | None, tags: Mapping[str, str]) -> str:
    """Label one summary group by the snapshot that first published it."""
    if commit is None:
        return 'Not in published commits:'
    identity = commit if verbose_enabled() else commit[:12]
    versions = ', '.join(escape(name) for name, target in tags.items() if target == commit)
    if versions:
        return f'First published in {versions} ({identity}):'
    return f'First published in commit {identity}:'


def correction_text(release: str) -> str:
    """Explain how to remove findings from published history before reveal.

    Args:
        release: Escaped release label or commit ID where correction starts.

    Returns:
        Shared correction guidance suitable for publish, restore, and reveal.
    """
    return (
        'To remove them instead, cancel, correct the published history from '
        f'{release}, and recreate the repository before revealing. See '
        '"Sensitive terms in published history" in the guide.'
    )


def earliest_release(
    result: _audit.HistoryAudit, commits: Sequence[str], tags: Mapping[str, str]
) -> str:
    """Name the oldest published snapshot witnessing a finding.

    Args:
        result: Audit whose evidence identifies affected commits.
        commits: Complete audited history, ordered newest first.
        tags: Published tag names mapped to their target commits.

    Returns:
        Escaped tag names, a short commit ID, or a fallback when no commit
        witnesses a finding.
    """
    witnesses = {item.commit for item in result.evidence if item.commit is not None}
    for commit in reversed(commits):
        if commit in witnesses:
            names = ', '.join(escape(name) for name, target in tags.items() if target == commit)
            return names or commit[:12]
    return _EARLIEST_UNKNOWN


def finding_groups(
    result: _audit.HistoryAudit,
    configs: tuple[GlobalConfig, ProjectConfig],
    derived: Collection[str],
    commits: Sequence[str],
    tags: Mapping[str, str],
) -> list[str]:
    """Group warning categories by witness snapshot with escaped, styled evidence.

    Args:
        result: Audited findings and their witness locations.
        configs: Global and project rules used to label each finding's origin.
        derived: Private terms derived from the active environment.
        commits: Complete audited history, ordered newest first.
        tags: Published tag names mapped to their target commits.

    Returns:
        Display lines with category and snapshot headings, including separating
        blank lines. Findings without a witness commit appear last.
    """
    global_config, project = configs
    lines: list[str] = []
    for rule_class, heading in HISTORY_LABELS.items():
        groups: dict[str | None, list[_audit.HistoryEvidence]] = {}
        for item in result.evidence:
            if item.finding.rule_class is rule_class:
                groups.setdefault(item.commit, []).append(item)
        if not groups:
            continue
        lines.extend(('', style(style(f'Warning: {heading}', 'warning'), 'label')))
        for commit in (*commits, None):
            if commit not in groups:
                continue
            lines.extend(('', _snapshot_heading(commit, tags)))
            for item in groups[commit]:
                origins = rule_labels(GateHit(*item.finding), global_config, project, derived)
                lines.extend(('', *_finding_lines(item, origins)))
    if lines:
        lines.append('')
    return lines


def history_reminder(findings: Collection[_audit.AuditFinding]) -> str:
    """Count distinct published-history terms and locations in reveal guidance.

    Args:
        findings: Findings across all audited rule classes.

    Returns:
        A one-line reminder naming distinct term and location counts.
    """
    terms = _counted(len({finding.term for finding in findings}), 'flagged term')
    locations = _counted(len({finding.unit for finding in findings}), 'location')
    return (
        f'Published history contains {terms} in {locations}. pubanon repo reveal asks '
        'before making them public; pubanon audit shows them.'
    )


def rules_line(
    findings: Iterable[GateHit | _audit.AuditFinding],
    configs: tuple[GlobalConfig, ProjectConfig],
    derived: Collection[str],
) -> str:
    """Collapse the configured origins of findings into one rules line.

    Args:
        findings: History findings or gate hits; only rule class and term
            matter.
        configs: Global and project rules used to label each finding's origin.
        derived: Private terms derived from the active environment.

    Returns:
        `Rules: ` followed by each configured source and its entry numbers.
    """
    global_config, project = configs
    entries: dict[str, set[int]] = {}
    for finding in findings:
        for label in rule_labels(GateHit(*finding), global_config, project, derived):
            source, separator, number = label.rpartition(' entry ')
            if separator:
                entries.setdefault(source, set()).add(int(number))
            else:
                entries.setdefault(label, set())
    parts: list[str] = []
    for source in (*_RULE_SOURCES, *(name for name in entries if name not in _RULE_SOURCES)):
        if source not in entries:
            continue
        numbers = sorted(entries[source])
        if not numbers:
            parts.append(source)
            continue
        word = 'entry' if len(numbers) == 1 else 'entries'
        parts.append(f'{source} {word} {", ".join(str(number) for number in numbers)}')
    return 'Rules: ' + '; '.join(parts)


def summary_block(
    headline: str,
    counts: tuple[int, int],
    groups: Sequence[SummaryGroup],
    *,
    unlimited: bool = False,
) -> list[str]:
    """Render counted findings as aligned rows under escaped group headings.

    Args:
        headline: Headline text before the counts.
        counts: Distinct terms and distinct locations.
        groups: Escaped headings, each with rows of an escaped label, unescaped
            terms, and escaped detail lines printed under the row.
        unlimited: Show every row instead of stopping after 20.

    Returns:
        Styled display lines, ending with a count of omitted rows when any.
    """
    terms, locations = counts
    title = f'{headline}: {_counted(terms, "term")} in {_counted(locations, "location")}'
    labels = [label for _heading, rows in groups for label, _terms, _details in rows]
    width = max((len(label) for label in labels if len(label) <= _SUMMARY_LABEL_WIDTH), default=0)
    limit = None if unlimited else _SUMMARY_ROW_LIMIT
    lines = [style(style(title, 'warning'), 'label')]
    shown = 0
    for heading, rows in groups:
        if shown == limit:
            break
        lines.append('  ' + style(heading, 'label'))
        for label, row_terms, details in rows:
            if shown == limit:
                break
            padded = label.ljust(width) if len(label) <= _SUMMARY_LABEL_WIDTH else label
            names = ', '.join(style(escape(term), 'warning') for term in row_terms)
            lines.append(f'    {padded}  {names}')
            lines.extend(f'      {line}' for line in details)
            shown += 1
    if len(labels) > shown:
        lines.append(f'  and {_counted(len(labels) - shown, "more row")}')
    return lines


def _summary_block(
    evidence: Sequence[_audit.HistoryEvidence],
    headline: str,
    commits: Sequence[str],
    tags: Mapping[str, str],
) -> list[str]:
    """Render one rule class as location rows grouped by first-published snapshot."""
    groups: dict[str | None, dict[str, list[str]]] = {}
    for item in evidence:
        units = groups.setdefault(item.commit, {})
        units.setdefault(item.finding.unit, []).append(item.finding.term)
    ordered: list[str | None] = [commit for commit in reversed(commits) if commit in groups]
    ordered.extend(commit for commit in groups if commit is not None and commit not in ordered)
    if None in groups:
        ordered.append(None)
    counts = (
        len({item.finding.term for item in evidence}),
        len({item.finding.unit for item in evidence}),
    )
    return summary_block(
        headline,
        counts,
        [
            (
                _summary_heading(commit, tags),
                [
                    (escape(_location_label(unit)), unit_terms, ())
                    for unit, unit_terms in groups[commit].items()
                ],
            )
            for commit in ordered
        ],
    )


def finding_summary(
    result: _audit.HistoryAudit,
    configs: tuple[GlobalConfig, ProjectConfig],
    derived: Collection[str],
    commits: Sequence[str],
    tags: Mapping[str, str],
) -> list[str]:
    """Summarize findings by rule class and first-published snapshot.

    Args:
        result: Audited findings and their witness locations.
        configs: Global and project rules used to label each finding's origin.
        derived: Private terms derived from the active environment.
        commits: Complete audited history, ordered newest first.
        tags: Published tag names mapped to their target commits.

    Returns:
        Escaped, styled display lines, or an empty list when there are no
        findings.
    """
    lines: list[str] = []
    for rule_class, headline in _SUMMARY_HEADLINES.items():
        evidence = [item for item in result.evidence if item.finding.rule_class is rule_class]
        if not evidence:
            continue
        lines.extend(_summary_block(evidence, headline, commits, tags))
        rules = rules_line((item.finding for item in evidence), configs, derived)
        lines.append('  ' + escape(rules))
    if lines:
        lines.append('  pubanon audit shows each match in context.')
    return lines
