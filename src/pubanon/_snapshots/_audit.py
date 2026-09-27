"""Audit published history under the current content and identity rules."""

import hashlib
import json
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

from pubanon._config import GlobalConfig, Paths, ProjectConfig, rule_labels
from pubanon._errors import PubanonError
from pubanon._gate import GateHit, RuleClass, first_match, gate_unit
from pubanon._run import OID_HEX, git_failure_fact, run_git
from pubanon._snapshots._source import (
    Entry,
    Snapshot,
    decode_tree_path,
    tree_mode,
    validate_symlink_graph,
)
from pubanon._snapshots._store import store_path

_BATCH_HEADER_FIELD_COUNT = 3
_DIFF_METADATA_FIELD_COUNT = 5
_HEADER_FIELD_PATTERN = re.compile(rb'[!-~]+')
_OID_PATTERN = re.compile(OID_HEX.encode())
_SIGNATURE_FIELDS = frozenset({b'gpgsig', b'gpgsig-sha256'})
_STRUCTURAL_HEADER_FIELDS = frozenset(
    {b'author', b'committer', b'parent', b'tree', *_SIGNATURE_FIELDS}
)
_SYMLINK_MODE = 0o120000
_ZERO_MODE = b'000000'


class AuditFinding(NamedTuple):
    """Identify one current-rule match in published history.

    Attributes:
        unit: Published-history unit containing the match.
        rule_class: Publication-impact class of the matched rule.
        term: Active rule term found in the unit.
    """

    unit: str
    rule_class: RuleClass
    term: str


class HistoryEvidence(NamedTuple):
    """Locate one deterministic witness for a historical finding.

    Attributes:
        finding: Original deduplicated finding; evidence does not affect its
            digest.
        commit: Full witness commit ID, or None for a tag without a commit
            target.
        location: Byte unit and zero-based, end-exclusive matching offsets.
        label: Human-readable location, with byte offsets for binary content.
        excerpt: Bounded source context, or None for binary content.
        excerpt_span: Character offsets of the matched source in `excerpt`, or
            None when the byte match cannot be highlighted as text.
        description: Combined location and context for compact diagnostics.
            Callers escape all evidence for terminal output.
    """

    finding: AuditFinding
    commit: str | None
    location: str
    label: str
    excerpt: str | None = None
    excerpt_span: tuple[int, int] | None = None

    @property
    def description(self) -> str:
        """Combine the witness location and optional source text."""
        if self.excerpt is None:
            return self.label
        return f'{self.label}; text {self.excerpt}'


class HistoryAudit(NamedTuple):
    """Collect content findings and publication-structure problems.

    Attributes:
        findings: Deduplicated current-rule matches in deterministic order.
        identity_problems: Ordered problems with configured publication
            identities, commit headers, signatures, history topology, or tags.
        evidence: First witness for each finding, in finding order.
    """

    findings: tuple[AuditFinding, ...]
    identity_problems: tuple[str, ...]
    evidence: tuple[HistoryEvidence, ...]


class _Change(NamedTuple):
    """Describe one validated raw changed-path record."""

    raw_path: bytes
    path: str
    old_mode: int | None
    old_oid: bytes | None
    new_mode: int | None
    new_oid: bytes | None


class _HistoryEntry(NamedTuple):
    """Retain one active published path occurrence."""

    mode: int
    kind: str
    oid: bytes
    data: bytes


class _ParsedCommit(NamedTuple):
    """Retain validated structure and gateable values for one commit."""

    tree: str | None
    parents: tuple[str, ...]
    header_values: tuple[tuple[str, bytes], ...]
    problems: tuple[str, ...]


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


def _logical_commit_fields(
    oid: str,
    headers: bytes,
) -> tuple[tuple[tuple[bytes, bytes], ...], tuple[str, ...]]:
    """Join LF-delimited continuation records to their owning fields."""
    logical_fields: list[tuple[bytes, bytes]] = []
    problems: list[str] = []
    current_field: int | None = None
    for record in headers.split(b'\n'):
        if record.startswith(b' '):
            if current_field is None:
                problems.append(f'{oid[:12]}: orphan commit header continuation')
            else:
                name, value = logical_fields[current_field]
                logical_fields[current_field] = (name, value + b'\n' + record)
            continue
        current_field = None
        name, separator, value = record.partition(b' ')
        if not separator or _HEADER_FIELD_PATTERN.fullmatch(name) is None:
            problems.append(f'{oid[:12]}: malformed commit header')
            continue
        logical_fields.append((name, value))
        current_field = len(logical_fields) - 1
    return tuple(logical_fields), tuple(problems)


def _match_context(
    unit: str, label: str, data: bytes, start: int, end: int
) -> tuple[str, str | None, tuple[int, int] | None]:
    """Locate a text witness and excerpt, retaining byte locations for binary data."""
    if b'\x00' in data:
        return f'{label} bytes {start}:{end}', None, None
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        return f'{label} bytes {start}:{end}', None, None
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
    line = data[:start].count(b'\n') + 1
    if label == 'blob':
        label = f'{unit.removeprefix("path:")}:{line}'
    elif label == 'message':
        label = f'commit message line {line}'
    return label, excerpt, span


def _add_findings(
    findings: dict[AuditFinding, HistoryEvidence],
    unit: str,
    data: bytes,
    rules: Mapping[RuleClass, Collection[str]],
    witness: tuple[str | None, str],
) -> None:
    """Retain the first current-rule witness for one history unit."""
    commit, location = witness
    for hit in gate_unit(unit, data, rules):
        finding = AuditFinding(*hit)
        span = first_match(data, hit.term)
        if span is None:
            raise PubanonError('gate match location could not be determined')
        start, end = span
        findings.setdefault(
            finding,
            HistoryEvidence(
                finding,
                commit,
                f'{location} bytes {start}:{end}',
                *_match_context(unit, location, data, start, end),
            ),
        )


def _entry_findings(
    path: str,
    data: bytes,
    rules: Mapping[RuleClass, Collection[str]],
) -> dict[AuditFinding, HistoryEvidence]:
    """Gate one active path occurrence, preferring filename witnesses."""
    findings: dict[AuditFinding, HistoryEvidence] = {}
    unit = f'path:{path}'
    _add_findings(findings, unit, path.encode(), rules, (None, 'filename'))
    _add_findings(findings, unit, data, rules, (None, 'blob'))
    return findings


def _apply_change(
    current: dict[bytes, _HistoryEntry],
    active_findings: dict[bytes, dict[AuditFinding, HistoryEvidence]],
    change: _Change,
    objects: Mapping[bytes, tuple[bytes, bytes] | None],
    rules: Mapping[RuleClass, Collection[str]],
) -> dict[AuditFinding, HistoryEvidence] | None:
    """Apply one changed occurrence to current in-memory history state."""
    previous = current.get(change.raw_path)
    if change.old_mode is None:
        if previous is not None:
            raise PubanonError('Git returned an inconsistent published history diff')
    elif previous is None or previous.mode != change.old_mode or previous.oid != change.old_oid:
        raise PubanonError('Git returned an inconsistent published history diff')
    if change.new_mode is None or change.new_oid is None:
        current.pop(change.raw_path)
        active_findings.pop(change.raw_path, None)
        return None
    object_value = objects.get(change.new_oid)
    if object_value is None or object_value[0] != b'blob':
        raise PubanonError('published blob object could not be read')
    data = object_value[1]
    kind = 'symlink' if change.new_mode == _SYMLINK_MODE else 'blob'
    current[change.raw_path] = _HistoryEntry(change.new_mode, kind, change.new_oid, data)
    occurrence_findings = _entry_findings(change.path, data, rules)
    active_findings[change.raw_path] = occurrence_findings
    return occurrence_findings


def _object_oid(raw_oid: bytes, *, absent: bool, oid_length: int) -> bytes | None:
    """Parse one full raw-diff object ID."""
    if len(raw_oid) != oid_length or _OID_PATTERN.fullmatch(raw_oid) is None:
        raise PubanonError('Git returned a malformed tree object ID')
    if absent:
        if raw_oid != b'0' * oid_length:
            raise PubanonError('Git returned an inconsistent changed entry')
        return None
    if raw_oid == b'0' * oid_length:
        raise PubanonError('Git returned an inconsistent changed entry')
    return raw_oid


def _object_queries(
    raw_commits: Sequence[bytes],
    parsed_diff: Sequence[tuple[bytes, tuple[_Change, ...]]],
    tags: Mapping[str, str],
    focused_tags: Mapping[str, str],
) -> tuple[bytes, ...]:
    """Collect unique batch queries in deterministic first-use order."""
    queries: list[bytes] = []
    seen: set[bytes] = set()

    def _add(raw_oid: bytes) -> None:
        if raw_oid not in seen:
            seen.add(raw_oid)
            queries.append(raw_oid)

    for raw_oid in raw_commits:
        _add(raw_oid)
    for _commit, changes in parsed_diff:
        for change in changes:
            if change.new_oid is not None:
                _add(change.new_oid)
    for oid in (*tags.values(), *focused_tags.values()):
        try:
            raw_oid = oid.encode('ascii')
        except UnicodeEncodeError:
            continue
        if _OID_PATTERN.fullmatch(raw_oid) is not None:
            _add(raw_oid)
    return tuple(queries)


def _ordered_audit(
    findings: dict[AuditFinding, HistoryEvidence],
    identity_problems: Sequence[str],
) -> HistoryAudit:
    """Order one projected audit exactly as the established result."""
    ordered_findings = tuple(
        sorted(findings, key=lambda finding: (finding.unit, finding.rule_class.name, finding.term))
    )
    return HistoryAudit(
        ordered_findings,
        tuple(identity_problems),
        tuple(findings[finding] for finding in ordered_findings),
    )


def _parent_oids(oid: str, values: Sequence[bytes]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate and decode one commit's ordered parent IDs."""
    parents: list[str] = []
    problems: list[str] = []
    for value in values:
        if _OID_PATTERN.fullmatch(value) is None:
            problems.append(f'{oid[:12]}: invalid parent object ID')
        else:
            parents.append(value.decode('ascii'))
    return tuple(parents), tuple(problems)


def _parse_change(metadata: bytes, raw_path: bytes, oid_length: int) -> _Change:
    """Parse one exact raw changed-path record."""
    fields = metadata.removeprefix(b':').split(b' ')
    if not metadata.startswith(b':') or len(fields) != _DIFF_METADATA_FIELD_COUNT:
        raise PubanonError('Git returned a malformed changed entry')
    raw_old_mode, raw_new_mode, raw_old_oid, raw_new_oid, status = fields
    path = decode_tree_path(raw_path)
    old_mode = None if raw_old_mode == _ZERO_MODE else tree_mode(raw_old_mode, path)
    new_mode = None if raw_new_mode == _ZERO_MODE else tree_mode(raw_new_mode, path)
    old_oid = _object_oid(raw_old_oid, absent=old_mode is None, oid_length=oid_length)
    new_oid = _object_oid(raw_new_oid, absent=new_mode is None, oid_length=oid_length)
    if status == b'A':
        valid = old_mode is None and new_mode is not None
    elif status == b'D':
        valid = old_mode is not None and new_mode is None
    elif status in {b'M', b'T'}:
        valid = old_mode is not None and new_mode is not None
    else:
        valid = False
    if not valid:
        raise PubanonError('Git returned an inconsistent changed entry')
    return _Change(raw_path, path, old_mode, old_oid, new_mode, new_oid)


def _parse_commit_ids(commits: Sequence[str]) -> tuple[bytes, ...]:
    """Validate one published commit sequence before protocol framing."""
    raw_commits: list[bytes] = []
    for oid in commits:
        try:
            raw_oid = oid.encode('ascii')
        except UnicodeEncodeError as exc:
            raise PubanonError('Git returned an invalid published commit ID') from exc
        if _OID_PATTERN.fullmatch(raw_oid) is None:
            raise PubanonError('Git returned an invalid published commit ID')
        raw_commits.append(raw_oid)
    if len({len(raw_oid) for raw_oid in raw_commits}) > 1:
        raise PubanonError('published history contains mismatched object IDs')
    return tuple(raw_commits)


def _parse_diff(
    output: bytes,
    root_to_tip: Sequence[bytes],
) -> tuple[tuple[bytes, tuple[_Change, ...]], ...]:
    """Parse exact NUL-delimited `diff-tree --stdin` output."""
    if not output.endswith(b'\0'):
        raise PubanonError('Git returned an unterminated published history diff')
    tokens = output[:-1].split(b'\0')
    offset = 0
    parsed: list[tuple[bytes, tuple[_Change, ...]]] = []
    oid_length = len(root_to_tip[0])
    for commit in root_to_tip:
        if offset >= len(tokens) or tokens[offset] != commit:
            raise PubanonError('Git returned a mismatched published commit boundary')
        offset += 1
        changes: list[_Change] = []
        seen_paths: set[bytes] = set()
        while offset < len(tokens) and tokens[offset].startswith(b':'):
            metadata = tokens[offset]
            offset += 1
            if offset >= len(tokens):
                raise PubanonError('Git returned a changed entry without a path')
            raw_path = tokens[offset]
            offset += 1
            change = _parse_change(metadata, raw_path, oid_length)
            if raw_path in seen_paths:
                raise PubanonError('Git returned duplicate changed entries')
            seen_paths.add(raw_path)
            changes.append(change)
        parsed.append((commit, tuple(changes)))
    if offset != len(tokens):
        raise PubanonError('Git returned trailing published history diff data')
    return tuple(parsed)


def _parse_object_response(
    output: bytes,
    offset: int,
    query: bytes,
) -> tuple[tuple[bytes, bytes] | None, int]:
    """Parse one exact `cat-file --batch` response."""
    header_end = output.find(b'\n', offset)
    if header_end < 0:
        raise PubanonError('Git returned an unterminated published object header')
    raw_header = output[offset:header_end]
    if raw_header == query + b' missing':
        return None, header_end + 1
    header = raw_header.split(b' ')
    if len(header) != _BATCH_HEADER_FIELD_COUNT:
        raise PubanonError('Git returned a malformed published object header')
    actual_oid, object_type, raw_size = header
    if actual_oid != query or _OID_PATTERN.fullmatch(actual_oid) is None:
        raise PubanonError('Git returned a mismatched published object')
    if object_type not in {b'blob', b'commit', b'tag', b'tree'}:
        raise PubanonError('Git returned an unsupported published object type')
    if re.fullmatch(rb'0|[1-9][0-9]*', raw_size) is None:
        raise PubanonError('Git returned a malformed published object size')
    size = int(raw_size)
    data_start = header_end + 1
    data_end = data_start + size
    if data_end >= len(output) or output[data_end : data_end + 1] != b'\n':
        raise PubanonError('Git returned a truncated published object')
    return (object_type, output[data_start:data_end]), data_end + 1


def _read_history_diff(
    store: Path,
    raw_commits: Sequence[bytes],
) -> tuple[tuple[bytes, tuple[_Change, ...]], ...]:
    """Read one explicit-boundary changed-entry stream root to tip."""
    if not raw_commits:
        return ()
    root_to_tip = tuple(reversed(raw_commits))
    diff = run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'diff-tree',
            '--stdin',
            '--always',
            '--root',
            '-r',
            '-z',
            '--raw',
            '--no-abbrev',
            '--no-renames',
        ],
        cwd=store,
        input_bytes=b''.join(raw_oid + b'\n' for raw_oid in root_to_tip),
        check=False,
    )
    if diff.returncode != 0:
        raise PubanonError('published history diff read failed', fact=git_failure_fact(diff))
    return _parse_diff(diff.stdout, root_to_tip)


def _single_header_value(
    oid: str,
    field: str,
    values: Sequence[bytes],
) -> tuple[bytes | None, str | None]:
    """Require one value for a structural commit header."""
    if not values:
        return None, f'{oid[:12]}: missing {field} header'
    if len(values) > 1:
        return None, f'{oid[:12]}: duplicate {field} header'
    return values[0], None


def _parse_commit_headers(oid: str, headers: bytes, identity: bytes) -> _ParsedCommit:
    """Parse one commit's LF-delimited header records."""
    logical_fields, field_problems = _logical_commit_fields(oid, headers)
    problems = list(field_problems)

    trees = tuple(value for name, value in logical_fields if name == b'tree')
    raw_parents = tuple(value for name, value in logical_fields if name == b'parent')
    parents, parent_problems = _parent_oids(oid, raw_parents)
    problems.extend(parent_problems)
    header_values = tuple(
        (name.decode('ascii'), value)
        for name, value in logical_fields
        if name not in _STRUCTURAL_HEADER_FIELDS
    )

    tree: str | None = None
    raw_tree, tree_problem = _single_header_value(oid, 'tree', trees)
    if tree_problem is not None:
        problems.append(tree_problem)
    elif raw_tree is not None and _OID_PATTERN.fullmatch(raw_tree) is None:
        problems.append(f'{oid[:12]}: invalid tree object ID')
    elif raw_tree is not None:
        tree = raw_tree.decode('ascii')

    for field in ('author', 'committer'):
        values = tuple(value for name, value in logical_fields if name == field.encode('ascii'))
        value, problem = _single_header_value(oid, field, values)
        if problem is not None:
            problems.append(problem)
        elif value is not None and re.fullmatch(identity, value) is None:
            problems.append(f'{oid[:12]}: invalid {field} identity')
        elif value is not None:
            header_values += ((field, value.rsplit(b' ', 2)[0]),)

    if any(name in _SIGNATURE_FIELDS for name, _value in logical_fields):
        problems.append(f'{oid[:12]}: commit is signed')
    return _ParsedCommit(tree, parents, header_values, tuple(problems))


def _audit_commit_objects(
    commits: Sequence[str],
    objects: Mapping[bytes, tuple[bytes, bytes] | None],
    focused: frozenset[str],
    rules: Mapping[RuleClass, Collection[str]],
    global_config: GlobalConfig,
) -> tuple[
    dict[AuditFinding, HistoryEvidence],
    dict[AuditFinding, HistoryEvidence],
    dict[str, _ParsedCommit],
]:
    """Gate commit metadata and messages and classify structural problems."""
    full_findings: dict[AuditFinding, HistoryEvidence] = {}
    focused_findings: dict[AuditFinding, HistoryEvidence] = {}
    parsed_by_commit: dict[str, _ParsedCommit] = {}
    identity = (
        re.escape(f'{global_config.anon_name} <{global_config.anon_email}>'.encode())
        + rb' [0-9]+ \+0000'
    )
    for oid in commits:
        object_value = objects.get(oid.encode('ascii'))
        if object_value is None or object_value[0] != b'commit':
            raise PubanonError('published commit object could not be read')
        headers, separator, message = object_value[1].partition(b'\n\n')
        if not separator:
            raise PubanonError('Git returned a malformed published commit')
        unit = f'{oid[:12]}:message'
        _add_findings(full_findings, unit, message, rules, (oid, 'message'))
        if oid in focused:
            _add_findings(focused_findings, unit, message, rules, (oid, 'message'))
        parsed = _parse_commit_headers(oid, headers, identity)
        for field, value in parsed.header_values:
            unit = f'{oid[:12]}:header:{field}'
            _add_findings(full_findings, unit, value, rules, (oid, f'header {field} value'))
            if oid in focused:
                _add_findings(focused_findings, unit, value, rules, (oid, f'header {field} value'))
        parsed_by_commit[oid] = parsed
    return full_findings, focused_findings, parsed_by_commit


def _tag_audit(
    tags: Mapping[str, str],
    objects: Mapping[bytes, tuple[bytes, bytes] | None],
    rules: Mapping[RuleClass, Collection[str]],
    history_oids: Collection[str],
) -> tuple[dict[AuditFinding, HistoryEvidence], list[str]]:
    """Gate and classify one tag projection."""
    findings: dict[AuditFinding, HistoryEvidence] = {}
    identity_problems: list[str] = []
    for name, oid in tags.items():
        _add_findings(
            findings,
            f'tag:{name}',
            name.encode(),
            rules,
            (oid if oid in history_oids else None, 'tag name'),
        )
        try:
            raw_oid = oid.encode('ascii')
        except UnicodeEncodeError:
            raw_oid = b''
        object_value = objects.get(raw_oid)
        if _OID_PATTERN.fullmatch(raw_oid) is None or object_value is None:
            identity_problems.append(f'tag:{name}: tag does not point directly to a commit')
            continue
        object_type, _data = object_value
        if object_type != b'commit':
            identity_problems.append(f'tag:{name}: tag does not point directly to a commit')
            continue
        if oid not in history_oids:
            identity_problems.append(f'tag:{name}: tag target is outside main history')
    return findings, identity_problems


def _topology_problems(
    commits: Sequence[str],
    parsed: Mapping[str, _ParsedCommit],
    tip: str,
) -> tuple[str, ...]:
    """Classify deviations from one complete tip-to-root commit chain."""
    if not commits:
        return ()
    problems: list[str] = []
    if commits[0] != tip:
        problems.append('published history tip does not match declared tip')
    if len(set(commits)) != len(commits):
        problems.append('published history contains duplicate commits')
        return tuple(problems)
    for index, oid in enumerate(commits):
        parents = parsed[oid].parents
        if index == len(commits) - 1:
            if parents:
                problems.append(f'{oid[:12]}: oldest commit has a parent')
        elif len(parents) != 1:
            problems.append(f'{oid[:12]}: commit does not have exactly one parent')
        elif parents[0] != commits[index + 1]:
            problems.append(f'{oid[:12]}: commit parent does not match history')
    return tuple(problems)


def _walk_history(
    parsed_diff: Sequence[tuple[bytes, tuple[_Change, ...]]],
    objects: Mapping[bytes, tuple[bytes, bytes] | None],
    focused: frozenset[str],
    rules: Mapping[RuleClass, Collection[str]],
) -> tuple[dict[AuditFinding, HistoryEvidence], dict[AuditFinding, HistoryEvidence]]:
    """Apply changes once and project active findings at focused commits."""
    current: dict[bytes, _HistoryEntry] = {}
    symlinks: dict[bytes, Entry] = {}
    active_findings: dict[bytes, dict[AuditFinding, HistoryEvidence]] = {}
    full_findings: dict[AuditFinding, HistoryEvidence] = {}
    focused_findings: dict[AuditFinding, HistoryEvidence] = {}
    for raw_commit, changes in parsed_diff:
        symlinks_changed = False
        for change in changes:
            occurrence_findings = _apply_change(
                current,
                active_findings,
                change,
                objects,
                rules,
            )
            # Old modes have been checked against current state, including deletions.
            if change.new_mode == _SYMLINK_MODE:
                entry = current[change.raw_path]
                symlinks[change.raw_path] = Entry(change.path, entry.mode, entry.kind, entry.data)
                symlinks_changed = True
            else:
                symlinks_changed |= symlinks.pop(change.raw_path, None) is not None
            if occurrence_findings is not None:
                for finding, evidence in occurrence_findings.items():
                    full_findings.setdefault(
                        finding, evidence._replace(commit=raw_commit.decode('ascii'))
                    )
        if symlinks_changed:
            validate_symlink_graph(tuple(symlinks[path] for path in sorted(symlinks)))
        if raw_commit.decode('ascii') in focused:
            for occurrence_findings in active_findings.values():
                for finding, evidence in occurrence_findings.items():
                    focused_findings.setdefault(
                        finding, evidence._replace(commit=raw_commit.decode('ascii'))
                    )
    return full_findings, focused_findings


def changed_entries(old: Snapshot, new: Snapshot) -> Snapshot:
    """Return complete new or changed entries for one old-to-new tree comparison.

    Paths, bytes, kinds, and modes all participate in equality. Deletions have
    no new content to gate; renames include the complete entry at the new path.
    Unchanged entries remain historical exposure, even in rewritten commits.
    """
    previous = {entry.path: entry for entry in old}
    return tuple(entry for entry in new if previous.get(entry.path) != entry)


def exposure_digest(findings: Iterable[AuditFinding]) -> str:
    """Hash the current identity-critical history exposures.

    Args:
        findings: Current-rule history findings.

    Returns:
        Stable SHA-256 digest, or an empty string when nothing is exposed.
    """
    exposed = sorted(
        (finding.unit, finding.term)
        for finding in findings
        if finding.rule_class is RuleClass.IDENTITY_CRITICAL
    )
    if not exposed:
        return ''
    serialized = json.dumps(exposed, separators=(',', ':'))
    return hashlib.sha256(serialized.encode()).hexdigest()


def history_details(
    audit: HistoryAudit,
    g: GlobalConfig,
    p: ProjectConfig,
    derived: Collection[str],
    *,
    short_commits: bool = False,
) -> dict[str, str]:
    """Collect literal history evidence for operator review.

    Args:
        audit: Findings and evidence to describe.
        g: Global configuration supplying sensitive terms.
        p: Project configuration supplying additional terms.
        derived: Private path terms derived for this project.
        short_commits: Display 12-character commit IDs instead of full IDs.

    Returns:
        One labeled detail per finding in established order. Values contain
        terms and paths; callers escape these values for terminal display.
    """
    evidence = {item.finding: item for item in audit.evidence}
    details: dict[str, str] = {}
    for index, finding in enumerate(audit.findings, 1):
        item = evidence[finding]
        location = item.description
        if item.commit is not None:
            commit = item.commit[:12] if short_commits else item.commit
            location = f'commit {commit}; {location}'
        label = (
            'historical exposure'
            if finding.rule_class is RuleClass.IDENTITY_CRITICAL
            else 'policy mismatch'
        )
        sources = ', '.join(rule_labels(GateHit(*finding), g, p, derived))
        details[f'{label} {index}'] = f'{finding.unit}; {sources}; {location}; term {finding.term}'
    return details


def published_commits(
    paths: Paths,
    project: str,
    *,
    new: str,
    old: str | None = None,
) -> tuple[str, ...]:
    """List published commits in one full or incremental history range.

    Args:
        paths: Derived Pubanon paths.
        project: Registered project name.

        new: New published main tip.
        old: Optional earlier published main tip.

    Returns:
        Commit object IDs in Git revision-list order.

    Raises:
        PubanonError: The advisory store cannot be initialized or read, the
            revision walk fails, or Git returns a malformed commit object ID.
    """
    revision = new if old is None else f'{old}..{new}'
    store = store_path(paths, project)
    output = run_git(['--git-dir', str(store.absolute()), 'rev-list', revision], cwd=store).stdout
    commits: list[str] = []
    for raw_oid in output.splitlines():
        if _OID_PATTERN.fullmatch(raw_oid) is None:
            raise PubanonError('Git returned an invalid published commit ID')
        commits.append(raw_oid.decode('ascii'))
    return tuple(commits)


def read_objects(
    store: Path,
    queries: Sequence[bytes],
) -> dict[bytes, tuple[bytes, bytes] | None]:
    """Read exact object responses through one batch process.

    Args:
        store: Bare Git repository containing the objects.
        queries: Full ASCII object IDs in request order.

    Returns:
        Object type and raw contents for each ID, or None for missing objects.

    Raises:
        PubanonError: Git fails or returns inconsistent batch data.
    """
    if not queries:
        return {}
    result = run_git(
        ['--git-dir', str(store.absolute()), 'cat-file', '--batch'],
        cwd=store,
        input_bytes=b''.join(query + b'\n' for query in queries),
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError('published object read failed', fact=git_failure_fact(result))
    output = result.stdout
    objects: dict[bytes, tuple[bytes, bytes] | None] = {}
    offset = 0
    for query in queries:
        objects[query], offset = _parse_object_response(output, offset, query)
    if offset != len(output):
        raise PubanonError('Git returned trailing published object data')
    return objects


def audit_history_pair(  # noqa: PLR0913, PLR0917
    paths: Paths,
    project: str,
    commits: Sequence[str],
    tags: Mapping[str, str],
    focused_commits: Iterable[str],
    focused_tags: Mapping[str, str],
    rules: Mapping[RuleClass, Collection[str]],
    global_config: GlobalConfig,
    *,
    tip: str,
) -> tuple[HistoryAudit, HistoryAudit]:
    """Audit full history and one focused projection through one object walk.

    Invalid commit tree headers or chain topology skip path and blob scanning.
    Malformed or unsafe reconstructed tree data may raise `PubanonError`.

    Args:
        paths: Derived Pubanon paths.
        project: Registered project name.
        commits: Full duplicate-free tip-to-root commit chain, ending at the
            root. The first commit must be `tip`.
        tags: Published tag names mapped to direct object IDs.
        focused_commits: Subset of `commits` projected from the full walk, not
            an incremental replacement for that walk.
        focused_tags: Direct tags included in the focused projection.
        rules: Reusable current term collections grouped by rule class.
        global_config: Current publication identity expected in every commit.

        tip: Published main tip naming the first commit in `commits`; every tag
            target must belong to that main history.

    Returns:
        A `(full, focused)` tuple of audits, each containing deduplicated,
        sorted content findings and ordered identity problems. Commit-header
        problems follow focused commit membership, while topology problems
        enter both results. Callers must inspect the full audit before treating
        an empty focused result as evidence of content cleanliness.

    Raises:
        PubanonError: Published Git data or a requested projection is
            malformed.
    """
    store = store_path(paths, project)
    raw_commits = _parse_commit_ids(commits)
    focused_set = frozenset(focused_commits)
    if not focused_set.issubset(commits):
        raise PubanonError('focused history is not part of published history')
    objects = read_objects(store, _object_queries(raw_commits, (), tags, focused_tags))
    full_findings, focused_findings, parsed_by_commit = _audit_commit_objects(
        commits,
        objects,
        focused_set,
        rules,
        global_config,
    )
    topology_problems = _topology_problems(commits, parsed_by_commit, tip)
    if topology_problems or any(commit.tree is None for commit in parsed_by_commit.values()):
        path_findings: dict[AuditFinding, HistoryEvidence] = {}
        focused_path_findings: dict[AuditFinding, HistoryEvidence] = {}
    else:
        parsed_diff = _read_history_diff(store, raw_commits)
        remaining_queries = tuple(
            query
            for query in _object_queries(raw_commits, parsed_diff, tags, focused_tags)
            if query not in objects
        )
        objects.update(read_objects(store, remaining_queries))
        path_findings, focused_path_findings = _walk_history(
            parsed_diff, objects, focused_set, rules
        )
    full_findings.update(path_findings)
    focused_findings.update(focused_path_findings)

    full_identity = [problem for oid in commits for problem in parsed_by_commit[oid].problems]
    focused_identity = [
        problem
        for oid in commits
        if oid in focused_set
        for problem in parsed_by_commit[oid].problems
    ]
    full_identity.extend(topology_problems)
    focused_identity.extend(topology_problems)
    history_oids = frozenset(commits)
    tag_findings, tag_identity = _tag_audit(
        tags,
        objects,
        rules,
        history_oids,
    )
    full_findings.update(tag_findings)
    full_identity.extend(tag_identity)
    focused_tag_findings, focused_tag_identity = _tag_audit(
        focused_tags,
        objects,
        rules,
        history_oids,
    )
    focused_findings.update(focused_tag_findings)
    focused_identity.extend(focused_tag_identity)
    return (
        _ordered_audit(full_findings, full_identity),
        _ordered_audit(focused_findings, focused_identity),
    )


def audit_history(  # noqa: PLR0913, PLR0917
    paths: Paths,
    project: str,
    commits: Sequence[str],
    tags: Mapping[str, str],
    rules: Mapping[RuleClass, Collection[str]],
    g: GlobalConfig,
    *,
    tip: str,
) -> HistoryAudit:
    """Audit published objects against current content and identity rules.

    Invalid commit tree headers or chain topology skip path and blob scanning.
    Malformed or unsafe reconstructed tree data may raise `PubanonError`.
    Missing content findings in a structurally invalid result do not establish
    content cleanliness. Focused subset auditing uses `audit_history_pair` with
    this same full walk, not an incremental chain.

    Args:
        paths: Derived Pubanon paths.
        project: Registered project name.
        commits: Full duplicate-free tip-to-root commit chain, ending at the
            root. The first commit must be `tip`.
        tags: Published tag names mapped to direct object IDs.
        rules: Reusable current term collections grouped by rule class.
        g: Current publication identity expected in every commit.
        tip: Published main tip naming the first commit in `commits`; every tag
            target must belong to that main history.

    Returns:
        The full audit: deduplicated, sorted content findings followed by
        ordered identity problems. Commit-header and topology problems are
        included even when content scanning skips.

    Raises:
        PubanonError: Published Git data is malformed or cannot be read.
    """
    full, _focused = audit_history_pair(paths, project, commits, tags, (), {}, rules, g, tip=tip)
    return full
