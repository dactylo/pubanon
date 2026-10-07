"""Render historical findings grouped by published snapshot."""

from collections.abc import Collection, Mapping, Sequence

from pubanon._config import GlobalConfig, ProjectConfig, rule_labels
from pubanon._gate import GateHit, RuleClass
from pubanon._snapshots import _audit
from pubanon._terminal import color_enabled, escape, style, verbose_enabled

HISTORY_LABELS = {
    RuleClass.IDENTITY_CRITICAL: 'historical exposure',
    RuleClass.HYGIENE: 'policy mismatch',
}


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


def _snapshot_heading(commit: str | None, tags: Mapping[str, str]) -> str:
    """Label the witness snapshot with its published tags and commit ID."""
    identity = 'unknown'
    if commit is not None:
        identity = commit if verbose_enabled() else commit[:12]
        versions = ', '.join(name for name, target in tags.items() if target == commit)
        if versions:
            identity = f'{versions} ({identity})'
    return style(f'Published snapshot: {escape(identity)}', 'label')


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
