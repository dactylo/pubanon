"""Discover check names from bounded, static GitHub Actions workflows."""

import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeGuard, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from pubanon._snapshots import _source

type _Combination = dict[str, _Scalar]
type _Scalar = str | int | float | bool | None

_EXPRESSION = re.compile(r'\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}')
_REGULAR_MODES = frozenset({0o100644, 0o100755})
_WORKFLOW_PATH = re.compile(r'\.github/workflows/[^/]+\.ya?ml')


@dataclass(frozen=True)
class CheckCaution:
    """Explain why a discovered check needs review before being required.

    Attributes:
        name: Discovered GitHub check name.
        path: Workflow file defining this check.
        reasons: Trigger or condition limitations for ordinary branch pushes.
    """

    name: str
    path: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Discovery:
    """Contain ordered workflow check names and discovery limitations.

    Attributes:
        names: Unique check names in workflow and matrix declaration order.
        notes: Reasons the discovered list may be incomplete.
        cautions: Checks whose execution on a branch push needs explicit
            review.
    """

    names: tuple[str, ...]
    notes: tuple[str, ...]
    cautions: tuple[CheckCaution, ...] = ()


class _YamlLoader(Protocol):
    """Describe the typed part of the ruamel safe-loader boundary."""

    def load(self, stream: str, /) -> object:
        """Load one YAML document."""


def _as_mapping(value: object) -> Mapping[object, object] | None:
    """Narrow a parsed YAML value to a mapping."""
    if not isinstance(value, Mapping):
        return None
    return cast('Mapping[object, object]', value)


def _as_sequence(value: object) -> Sequence[object] | None:
    """Narrow a parsed YAML value to a non-string sequence."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    return cast('Sequence[object]', value)


def _format_scalar(value: _Scalar) -> str:
    """Render a static matrix scalar as a GitHub-style name component."""
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _is_scalar(value: object) -> TypeGuard[_Scalar]:
    """Return whether a YAML value is in the supported static scalar set."""
    if isinstance(value, str):
        return '${{' not in value
    return value is None or isinstance(value, (int, float, bool))


def _load_workflow(data: bytes) -> tuple[Mapping[object, object] | None, str | None]:
    """Parse one workflow mapping or return a diagnostic."""
    try:
        text = data.decode('utf-8')
        yaml = YAML(typ='safe', pure=True)
        yaml.version = (1, 2)
        loader = cast('_YamlLoader', yaml)
        document = loader.load(text)
    except (UnicodeDecodeError, YAMLError):
        return None, 'could not be parsed as YAML 1.2'
    root = _as_mapping(document)
    if root is None:
        return None, 'does not contain a workflow mapping'
    return root, None


def _matches(combination: _Combination, pattern: _Combination) -> bool:
    """Return whether all pattern pairs occur in a matrix combination."""
    return all(combination.get(key, object()) == value for key, value in pattern.items())


def _expand_matrix(
    axes: list[tuple[str, list[_Scalar]]],
    excludes: list[_Combination],
    includes: list[_Combination],
) -> tuple[list[_Combination] | None, str | None]:
    """Apply literal exclude and include entries to static axes."""
    if not axes:
        if excludes or not includes:
            return None, 'has a matrix without static axes or an include-only list'
        return [dict(entry) for entry in includes], None

    axis_names = tuple(name for name, _values in axes)
    combinations = [
        dict(zip(axis_names, values, strict=True))
        for values in itertools.product(*(values for _name, values in axes))
    ]
    combinations = [
        combination
        for combination in combinations
        if not any(_matches(combination, exclusion) for exclusion in excludes)
    ]

    original_count = len(combinations)
    for inclusion in includes:
        compatible = [
            index
            for index, combination in enumerate(combinations[:original_count])
            if all(
                key not in axis_names or combination[key] == item_value
                for key, item_value in inclusion.items()
            )
        ]
        if compatible:
            for index in compatible:
                combinations[index].update(inclusion)
        else:
            combinations.append(dict(inclusion))
    return combinations, None


def _matrix_axes(
    matrix: Mapping[object, object],
) -> tuple[list[tuple[str, list[_Scalar]]] | None, str | None]:
    """Read matrix axes while preserving their declaration order."""
    axes: list[tuple[str, list[_Scalar]]] = []
    for key, raw_values in matrix.items():
        if key in {'include', 'exclude'}:
            continue
        if not isinstance(key, str):
            return None, 'has a matrix axis with a non-string name'
        values = _as_sequence(raw_values)
        if values is None or not values:
            return None, f'has unsupported or expression-based values for matrix axis {key!r}'
        scalars: list[_Scalar] = []
        for item in values:
            if not _is_scalar(item):
                return None, f'has unsupported or expression-based values for matrix axis {key!r}'
            scalars.append(item)
        axes.append((key, scalars))
    return axes, None


def _matrix_entries(value: object) -> list[_Combination] | None:
    """Narrow an include or exclude value to static scalar mappings."""
    items = _as_sequence(value)
    if items is None:
        return None
    entries: list[_Combination] = []
    for item in items:
        mapping = _as_mapping(item)
        if mapping is None:
            return None
        entry: _Combination = {}
        for key, item_value in mapping.items():
            if not isinstance(key, str) or not _is_scalar(item_value):
                return None
            entry[key] = item_value
        entries.append(entry)
    return entries


def _matrix_lists(
    matrix: Mapping[object, object],
) -> tuple[tuple[list[_Combination], list[_Combination]] | None, str | None]:
    """Read literal matrix exclude and include lists."""
    excludes = _matrix_entries(matrix.get('exclude', []))
    if excludes is None:
        return None, 'has a non-literal or expression-based matrix exclude list'
    includes = _matrix_entries(matrix.get('include', []))
    if includes is None:
        return None, 'has a non-literal or expression-based matrix include list'
    return (excludes, includes), None


def _parse_matrix(value: object) -> tuple[list[_Combination] | None, str | None]:
    """Expand one supported static matrix or describe why it is unsupported."""
    matrix = _as_mapping(value)
    if matrix is None:
        return None, 'uses a dynamic or non-mapping matrix'
    axes, problem = _matrix_axes(matrix)
    if axes is None:
        return None, problem
    lists, problem = _matrix_lists(matrix)
    if lists is None:
        return None, problem
    excludes, includes = lists
    return _expand_matrix(axes, excludes, includes)


def _job_combinations(
    job: Mapping[object, object],
) -> tuple[list[_Combination] | None, str | None]:
    """Return one job's static matrix combinations."""
    strategy = job.get('strategy')
    if strategy is None:
        return [{}], None
    strategy_mapping = _as_mapping(strategy)
    if strategy_mapping is None:
        return None, 'strategy is not a supported mapping'
    if 'matrix' not in strategy_mapping:
        return [{}], None
    return _parse_matrix(strategy_mapping['matrix'])


def _push_cautions(value: object) -> tuple[str, ...]:
    """Describe literal filters that may prevent a workflow running on a branch push."""
    if value is None:
        return ()
    filters = _as_mapping(value)
    if filters is None:
        return ('push trigger could not be interpreted',)
    if ('tags' in filters or 'tags-ignore' in filters) and not (
        'branches' in filters or 'branches-ignore' in filters
    ):
        return ('workflow runs on tags, not branch pushes',)
    reasons: list[str] = []
    if ('branches' in filters and filters['branches'] != ['**']) or 'branches-ignore' in filters:
        reasons.append('workflow filters branches and may not run for the published branch')
    if 'paths' in filters or 'paths-ignore' in filters:
        reasons.append('workflow filters changed paths and may not run for every push')
    return tuple(reasons)


def _render_explicit_name(name: str, combination: _Combination) -> str | None:
    """Substitute supported matrix references in an explicit job name."""
    unresolved = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal unresolved
        key = match.group(1)
        if key not in combination:
            unresolved = True
            return match.group(0)
        return _format_scalar(combination[key])

    rendered = _EXPRESSION.sub(_replace, name)
    if unresolved or '${{' in rendered or '}}' in rendered:
        return None
    return rendered


def _job_names(job_id: str, job: Mapping[object, object]) -> tuple[list[str] | None, str | None]:
    """Resolve one supported job to its ordered check names."""
    if 'uses' in job:
        return None, 'reusable workflow jobs are not supported'
    has_explicit_name = 'name' in job
    raw_name = job.get('name', job_id)
    if not isinstance(raw_name, str) or not raw_name:
        return None, 'job name is not a non-empty static string'
    combinations, problem = _job_combinations(job)
    if combinations is None:
        return None, problem

    names: list[str] = []
    for combination in combinations:
        if not has_explicit_name:
            values = ', '.join(_format_scalar(value) for value in combination.values())
            names.append(f'{job_id} ({values})' if combination else job_id)
            continue
        rendered = _render_explicit_name(raw_name, combination)
        if rendered is None:
            return None, 'explicit name contains an unresolved expression'
        names.append(rendered)
    return names, None


def _workflow_cautions(events: object) -> tuple[str, ...]:
    """Describe a workflow's declared availability for ordinary branch pushes."""
    mapping = _as_mapping(events)
    if mapping is not None:
        triggers = tuple(mapping)
    elif isinstance(events, str):
        triggers = (events,)
    elif (sequence := _as_sequence(events)) is not None:
        triggers = tuple(sequence)
    else:
        return ('workflow trigger could not be determined',)
    if 'push' in triggers:
        return _push_cautions(mapping['push'] if mapping is not None else None)
    if triggers == ('workflow_dispatch',):
        return ('workflow starts manually; a push does not start it',)
    return ('workflow does not run on branch pushes',)


def _discover_workflow(
    entry: _source.Entry,
    names: list[str],
    notes: list[str],
    seen: set[str],
    cautions: list[CheckCaution],
) -> None:
    """Add discoverable names and notes from one workflow entry."""
    root, problem = _load_workflow(entry.data)
    if root is None:
        notes.append(f'{entry.path}: {problem}')
        return
    jobs = _as_mapping(root.get('jobs'))
    if jobs is None:
        notes.append(f'{entry.path}: does not contain a jobs mapping')
        return
    workflow_cautions = _workflow_cautions(root.get('on'))

    for raw_job_id, raw_job in jobs.items():
        job_id = raw_job_id if isinstance(raw_job_id, str) else repr(raw_job_id)
        context = f'{entry.path} job {job_id!r}'
        job = _as_mapping(raw_job)
        if not isinstance(raw_job_id, str) or job is None:
            notes.append(f'{context}: job definition is not a supported mapping')
            continue
        discovered, problem = _job_names(job_id, job)
        if discovered is None:
            notes.append(f'{context}: {problem}')
            continue
        reasons = workflow_cautions
        if 'if' in job:
            reasons += ('job has a condition and may be skipped',)
        for name in discovered:
            caution = CheckCaution(name, entry.path, reasons)
            if reasons and caution not in cautions:
                cautions.append(caution)
            if name in seen:
                notes.append(f'{context}: duplicate discovered check name {name!r}')
                continue
            seen.add(name)
            names.append(name)


def discover(snapshot: _source.Snapshot) -> Discovery:
    """Discover statically knowable check names from direct workflow blobs.

    The caller controls which committed files are present in `snapshot`. This
    function reads only direct `.github/workflows` YAML blobs and does not
    interpret general GitHub expressions or reusable workflows.

    Args:
        snapshot: Selected committed source entries to inspect.

    Returns:
        Ordered unique names, notes for unresolved jobs, and execution
        cautions.
    """
    names: list[str] = []
    notes: list[str] = []
    seen: set[str] = set()
    cautions: list[CheckCaution] = []

    for entry in snapshot:
        if (
            entry.kind != 'blob'
            or entry.mode not in _REGULAR_MODES
            or _WORKFLOW_PATH.fullmatch(entry.path) is None
        ):
            continue
        _discover_workflow(entry, names, notes, seen, cautions)

    return Discovery(tuple(names), tuple(notes), tuple(cautions))
