"""Read operator answers and escape text for display."""

from __future__ import annotations

import os
import re
import shlex
import sys
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePath, PureWindowsPath
from typing import TYPE_CHECKING, Literal, TextIO

from pubanon import _errors

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Sequence
    from datetime import datetime

    from pubanon._remote._host import CheckVerdict

if os.name == 'nt':
    import msvcrt

    _TERMINAL_ERRORS = (EOFError, OSError)
else:
    import readline
    import termios
    import tty

    _TERMINAL_ERRORS = (EOFError, OSError, termios.error)

PROJECT_COMMANDS: tuple[str, ...] = (
    'audit',
    'clean',
    'delete',
    'discard',
    'forget',
    'init',
    'inspect',
    'list',
    'package watch',
    'prepare',
    'publish',
    'release create',
    'release edit',
    'release list',
    'release show',
    'repo create',
    'repo restore',
    'repo reveal',
    'repo verify',
    'repo watch',
    'run',
    'show',
    'stage delete',
    'stage forget',
    'stage push',
    'stage repair',
    'stage status',
    'stage watch',
    'status',
)

_BYTE_ESCAPE_MAX = 0xFF
_CHOICE_PAIR_SIZE = 2
_COLOR_MODE: ContextVar[str] = ContextVar('color_mode', default='auto')
_GUIDANCE_IMPLICIT_PROJECT: ContextVar[str | None] = ContextVar(
    'guidance_implicit_project', default=None
)
_GUIDANCE_PROJECT: ContextVar[str | None] = ContextVar('guidance_project', default=None)
_GUIDANCE_WORKFLOW: ContextVar[bool] = ContextVar('guidance_workflow', default=False)
_NO = frozenset({'n'})
_SHORT_ESCAPE_MAX = 0xFFFF
_STYLES: dict[StyleKind, str] = {
    'plain': '',
    'note': '2',
    'label': '1',
    'heading': '1;36',
    'choice': '36',
    'success': '32',
    'warning': '33',
    'error': '1;31',
}
_VERBOSE: ContextVar[bool] = ContextVar('verbose', default=False)
_YES = frozenset({'y'})


type StyleKind = Literal[
    'plain', 'note', 'label', 'heading', 'choice', 'success', 'warning', 'error'
]


def _parse_choices(choices: Iterable[str]) -> list[tuple[str, str, str]]:
    """Return (word, letter, rendered) for each choice, using a [x] mark or the first letter."""
    parsed: list[tuple[str, str, str]] = []
    for choice in choices:
        if '[' in choice:
            before, _open, rest = choice.partition('[')
            letter, _close, after = rest.partition(']')
            word = before + letter + after
            rendered = choice
        else:
            word, letter, rendered = choice, choice[0], f'[{choice[0]}]{choice[1:]}'
        parsed.append((word, letter.casefold(), rendered))
    letters = [letter for _word, letter, _rendered in parsed]
    if len(set(letters)) != len(letters):
        raise ValueError(f'ambiguous choice letters: {choices}')
    return parsed


@contextmanager
def color_context(mode: str) -> Generator[None]:
    """Apply `auto`, `always`, or `never` styling within a command.

    Args:
        mode: `auto` uses each destination's terminal status and `NO_COLOR`;
            `always` and `never` override both.

    Raises:
        ValueError: The color mode is unknown.
    """
    if mode not in {'auto', 'always', 'never'}:
        raise ValueError(f'invalid color mode: {mode}')
    token = _COLOR_MODE.set(mode)
    try:
        yield
    finally:
        _COLOR_MODE.reset(token)


def color_enabled(*, stream: TextIO | None = None) -> bool:
    """Return whether the scoped color mode enables styling for the destination."""
    target = sys.stdout if stream is None else stream
    mode = _COLOR_MODE.get()
    return mode == 'always' or (
        mode == 'auto' and 'NO_COLOR' not in os.environ and target.isatty()
    )


def display_duration(seconds: int) -> str:
    """Render whole minutes when possible, otherwise seconds."""
    if seconds % 60 == 0:
        minutes = seconds // 60
        return f'{minutes} minute' + ('' if minutes == 1 else 's')
    return f'{seconds} seconds'


def display_time(value: datetime) -> str:
    """Render a timestamp in local time with seconds and its zone abbreviation."""
    return value.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')


def escape(text: str) -> str:
    """Escape terminal control characters and backslashes visibly."""
    pieces: list[str] = []
    for character in text:
        codepoint = ord(character)
        if character == '\\':
            pieces.append(r'\\')
        elif unicodedata.category(character) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'}:
            if codepoint <= _BYTE_ESCAPE_MAX:
                pieces.append(f'\\x{codepoint:02x}')
            elif codepoint <= _SHORT_ESCAPE_MAX:
                pieces.append(f'\\u{codepoint:04x}')
            else:
                pieces.append(f'\\U{codepoint:08x}')
        else:
            pieces.append(character)
    return ''.join(pieces)


def _read_key_posix(prompt: str) -> str:
    """Read and echo one key, restoring terminal settings on every exit."""
    if os.name == 'nt':
        raise OSError('POSIX terminal input is unavailable on Windows')
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor, termios.TCSANOW)
        termios.tcflush(descriptor, termios.TCIFLUSH)
        print(prompt, end='', flush=True)
        key = os.read(descriptor, 1)
        termios.tcflush(descriptor, termios.TCIFLUSH)
        if key == b'\x03':
            raise KeyboardInterrupt
        if not key or key == b'\x04':
            raise EOFError
        answer = key.decode('ascii', errors='replace')
        if answer in {'\r', '\n'}:
            return ''
        print(escape(answer), end='', flush=True)
        return answer
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
        print(flush=True)


def _read_key_windows(prompt: str, getwch: Callable[[], str], kbhit: Callable[[], bool]) -> str:
    """Read one console key, consuming both characters of an extended key."""
    try:
        while kbhit():
            getwch()
        print(prompt, end='', flush=True)
        answer = getwch()
        if answer in {'\x00', '\xe0'}:
            getwch()
            answer = '\x00'
        while kbhit():
            getwch()
        if answer == '\x03':
            raise KeyboardInterrupt
        if answer in {'', '\x04', '\x1a'}:
            raise EOFError
        if answer in {'\r', '\n'}:
            return ''
        print(escape(answer), end='', flush=True)
        return answer
    finally:
        print(flush=True)


def _read_key(prompt: str) -> str:
    """Read one menu key through the native terminal interface."""
    if os.name == 'nt':
        return _read_key_windows(prompt, msvcrt.getwch, msvcrt.kbhit)
    return _read_key_posix(prompt)


def _read(prompt: str, *, key: bool = False) -> str:
    """Read a line or menu key, converting terminal failures to pubanon errors."""
    if not sys.stdin.isatty():
        raise _errors.PubanonError(
            'this step needs an interactive terminal',
            recovery='rerun in an interactive terminal',
        )
    try:
        if key:
            return _read_key(prompt)
        if os.name != 'nt':
            # Enable editing without retaining answers in process-wide input history.
            readline.set_auto_history(False)
            # Readline must not count ANSI color sequences as visible columns.
            prompt = re.sub(r'\x1b\[[0-9;]*m', '\x01\\g<0>\x02', prompt)
        return input(prompt)
    except KeyboardInterrupt:
        raise _errors.OperationCanceled('Canceled.') from None
    except _TERMINAL_ERRORS as exc:
        raise _errors.PubanonError(
            'this step needs an interactive terminal',
            recovery='rerun in an interactive terminal',
        ) from exc


def display_check_result(verdict: CheckVerdict) -> str:
    """Render an aggregated check verdict without changing its acceptance rules."""
    if verdict.state != 'unacceptable':
        return {
            'missing': 'not found',
            'ambiguous': 'could not be determined',
            'pending': 'pending',
            'success': 'passed',
        }[verdict.state]
    words = {'cancelled': 'canceled', 'timed_out': 'timed out', 'action_required': 'needs action'}
    details = [
        words.get(conclusion, conclusion)
        for conclusion in verdict.detail.split(', ')
        if conclusion and conclusion != 'failure'
    ]
    return 'failed' + (f' ({escape(", ".join(details))})' if details else '')


def escape_lines(text: str) -> list[str]:
    """Split command output at line feeds and escape each line visibly.

    A carriage return just before a line feed ends the line with it. Every
    other control character, including vertical tabs, lone carriage returns,
    and Unicode line separators, stays in its line and is escaped. Lines that
    are empty or hold only spaces and tabs are dropped at the start and end.

    Args:
        text: Decoded command output.

    Returns:
        The escaped lines; empty when no line has other content.
    """
    lines = text.replace('\r\n', '\n').split('\n')
    while lines and not lines[0].strip(' \t'):
        del lines[0]
    while lines and not lines[-1].strip(' \t'):
        del lines[-1]
    return [escape(line) for line in lines]


@contextmanager
def guidance_context(
    *, project: str | None = None, implicit_project: str | None = None, workflow: bool = False
) -> Generator[None]:
    """Keep command advice scoped to a project or a composed workflow.

    A nested workflow inherits the current project and the directory-selected
    project. Tokens are restored when the command exits, including after a
    failure.

    Args:
        project: Selected project to include in command advice when needed.
        implicit_project: Project selected by the current directory.
        workflow: Suppress intermediate next-step advice.
    """
    selected = _GUIDANCE_PROJECT.set(project or _GUIDANCE_PROJECT.get())
    implicit = _GUIDANCE_IMPLICIT_PROJECT.set(implicit_project or _GUIDANCE_IMPLICIT_PROJECT.get())
    composed = _GUIDANCE_WORKFLOW.set(workflow or _GUIDANCE_WORKFLOW.get())
    try:
        yield
    finally:
        _GUIDANCE_WORKFLOW.reset(composed)
        _GUIDANCE_IMPLICIT_PROJECT.reset(implicit)
        _GUIDANCE_PROJECT.reset(selected)


@contextmanager
def output_encoding_context() -> Generator[None]:
    """Display unencodable CLI text visibly while retaining stream settings."""
    # Transitional protection while stdio may use a non-UTF-8 locale encoding.
    # PEP 686 defaults to UTF-8 mode in Python 3.15, but PYTHONUTF8=0 still
    # disables it; revisit when Python 3.15 becomes the minimum.
    configured: list[tuple[TextIO, Callable[..., object], str]] = []
    try:
        for stream in (sys.stdout, sys.stderr):
            if any(stream is previous for previous, _reconfigure, _errors in configured):
                continue
            reconfigure = getattr(stream, 'reconfigure', None)
            errors = getattr(stream, 'errors', None)
            if callable(reconfigure) and isinstance(errors, str):
                reconfigure(errors='backslashreplace')
                configured.append((stream, reconfigure, errors))
        yield
    finally:
        for _stream, reconfigure, errors in reversed(configured):
            reconfigure(errors=errors)


def path_text(path: PurePath) -> str:
    """Use forward slashes for display, preserving Windows namespace spelling."""
    text = str(path)
    if isinstance(path, PureWindowsPath) and text.replace('/', '\\').startswith(
        ('\\\\?\\', '\\\\.\\', '\\??\\')
    ):
        return text
    return path.as_posix()


def display_path(path: Path) -> str:
    """Escape an absolute display path, abbreviating the home directory as `~`."""
    absolute = path.absolute()
    rendered = path_text(absolute)
    if isinstance(absolute, PureWindowsPath) and rendered == str(absolute):
        # Namespace paths must not be abbreviated into ordinary home paths.
        return escape(rendered)
    try:
        relative = absolute.relative_to(Path.home())
    except ValueError:
        return escape(rendered)
    return escape('~' if not relative.parts else '~/' + path_text(relative))


def project_fallback(project: str, action: str, mode: str | None) -> str:
    """Direct an unrenderable project command to its registered directory.

    Args:
        project: Project name to escape for display.
        action: Command phrase and any arguments to escape for display.
        mode: Optional recovery flag to escape and retain in the instruction.
    """
    with_mode = f' with {escape(mode)}' if mode is not None else ''
    return (
        f'Choose project {escape(project)} from its registered directory, '
        f'then rerun the {escape(action)} action{with_mode}.'
    )


def shell_command(*args: str) -> str | None:
    """Render a shell command only when visible text preserves its arguments."""
    command = shlex.join(args)
    visible = command.replace('\\', '')
    return command if escape(visible) == visible else None


def guidance(text: str) -> str:
    """Qualify command recommendations and omit redundant project selections.

    Preserve explicit selections for other projects. Remove a selection for the
    project already implied by the current directory.

    Use only for advice written by pubanon, never file contents, command
    output, or publication metadata.
    """
    implicit_project = _GUIDANCE_IMPLICIT_PROJECT.get()
    project = _GUIDANCE_PROJECT.get() or implicit_project
    if project is None:
        return text

    def _qualify(match: re.Match[str]) -> str:
        command = match.group('command')
        rest = match.group('rest')
        if project == implicit_project:
            quoted = re.escape(shlex.quote(project))
            rest = re.sub(rf' --project {quoted}(?= |$)', '', rest)
            quoted_option = re.escape(shlex.quote(f'--project={project}'))
            return command + re.sub(rf' {quoted_option}(?= |$)', '', rest)
        if re.search(r" (?:'--project=|--project(?:=|\s))", rest):
            return command + rest
        option = shell_command(f'--project={project}')
        if option is None:
            action = command.removeprefix('pubanon ')
            mode = re.search(r' --(?:clean|continue|resolve)(?=\s|$)', rest)
            return project_fallback(
                project, action, mode.group().strip() if mode is not None else None
            )
        return f'{command} {option}{rest}'

    return re.sub(
        r'(?P<command>(?<![\w/\\])pubanon (?:'
        + '|'.join(re.escape(command) for command in PROJECT_COMMANDS)
        + r')(?![\w/-]))'
        + r'(?P<rest>(?:(?![`;\n,]| then | or | && ).)*)',
        _qualify,
        text,
    )


@contextmanager
def verbose_context(enabled: bool) -> Generator[None]:
    """Scope diagnostic output, restoring the previous setting on every exit."""
    token = _VERBOSE.set(enabled)
    try:
        yield
    finally:
        _VERBOSE.reset(token)


def style(text: str, kind: StyleKind = 'label', *, stream: TextIO | None = None) -> str:
    """Style terminal text according to the scoped color mode.

    Args:
        text: Already escaped display text.
        kind: One of the terminal palette roles `plain`, `note`, `label`,
            `heading`, `choice`, `success`, `warning`, or `error`.
        stream: Destination used for terminal detection; defaults to stdout.

    Returns:
        Styled text when enabled, otherwise plain text.
    """
    code = _STYLES[kind]
    if not text or not code or not color_enabled(stream=stream):
        return text
    return f'\x1b[{code}m{text}\x1b[0m'


def diagnostic(error: _errors.PubanonError, *, stream: TextIO | None = None) -> str:
    """Format an error or cancellation for terminal display.

    Args:
        error: Structured failure with already escaped details.
        stream: Destination used for terminal detection; defaults to stdout.

    Returns:
        A colored headline, indented facts, and a separate next step.
    """
    kind = error.diagnostic_kind if isinstance(error, _errors.OperationCanceled) else 'error'
    headline = f'error: {error}' if kind == 'error' else str(error)
    lines = [style(headline, kind, stream=stream)]
    if error.fact is not None:
        lines.extend('  ' + line for line in error.fact.split('\n'))
    for name, value in (
        ('Expected', error.expected),
        ('Found', error.actual),
    ):
        if value is not None:
            first, *rest = value.split('\n')
            lines.append(f'  {style(name + ":", stream=stream)} {first}')
            lines.extend(' ' * (len(name) + 4) + line for line in rest)
    recovery = error.recovery
    if isinstance(error, _errors.PublicationCanceled):
        recovery = recovery or 'pubanon publish'
    if recovery is not None:
        first, *rest = guidance(recovery).split('\n')
        lines.extend(
            ('', f'{style("Next:", stream=stream)} {style(first, "choice", stream=stream)}')
        )
        lines.extend('      ' + style(line, 'choice', stream=stream) for line in rest)
    return '\n'.join(lines)


def field(label: str, value: str, *, kind: StyleKind = 'plain') -> str:
    """Render a bold field label and optionally styled value.

    Args:
        label: Already escaped field name, without a trailing colon.
        value: Already escaped field value.
        kind: Presentation role for the value, using the roles accepted by
            `style`.
    """
    return f'{style(label + ":")} {style(value, kind)}'


def next_step(text: str) -> None:
    """Print the next command unless an enclosing workflow owns the next step."""
    if not text or _GUIDANCE_WORKFLOW.get():
        return
    print(f'{style("Next:")} {style(guidance(text), "choice")}')


def render_fields(
    rows: Sequence[tuple[str, str]], *, indent: int = 0, align_continuations: bool = True
) -> str:
    """Render already escaped field rows with bold labels and aligned values.

    Args:
        rows: Pairs of field labels without colons and field values. Later
            lines of a multi-line value align under its first line unless
            `align_continuations` is false.
        indent: Number of spaces before each label.
        align_continuations: Add value-column indentation to continuation
            lines; disable for blocks whose layout is already formatted.
    """
    width = max((len(label) for label, _value in rows), default=0)
    continuation = '\n' + ' ' * (indent + width + 2) if align_continuations else '\n'
    return '\n'.join(
        ' ' * indent
        + style(label + ':')
        + ' ' * (width - len(label) + 1)
        + value.replace('\n', continuation)
        for label, value in rows
    )


def fields(rows: Sequence[tuple[str, str]], *, indent: int = 0) -> None:
    """Print already escaped field rows with bold labels and aligned values.

    Args:
        rows: Pairs of field labels without colons and field values. Later
            lines of a multi-line value align under its first line.
        indent: Number of spaces before each label.
    """
    if rows:
        print(render_fields(rows, indent=indent))


def verbose_enabled() -> bool:
    """Return whether diagnostic output is enabled for the current command."""
    return _VERBOSE.get()


def detail_fields(rows: Sequence[tuple[str, str]], *, indent: int = 0) -> None:
    """Print already escaped aligned field rows only in verbose mode.

    Args:
        rows: Pairs of field labels without colons and field values.
        indent: Number of spaces before each label.
    """
    if verbose_enabled():
        fields(rows, indent=indent)


def heading(text: str, *, separate: bool = False) -> None:
    """Print a bold title, optionally separated from preceding content.

    Args:
        text: Already escaped section title.
        separate: Add a blank line before an existing section.
    """
    print(('\n' if separate else '') + style(text.removesuffix(':'), 'heading'))


def output(text: str = '', *, kind: StyleKind = 'plain') -> None:
    """Print one terminal message using its presentation role.

    Args:
        text: Already escaped display text.
        kind: Presentation role accepted by `style`.
    """
    print(style(text, kind))


def detail(text: str = '') -> None:
    """Print already escaped diagnostic text as a note only in verbose mode."""
    if verbose_enabled():
        output(text, kind='note')


def ask(
    question: str,
    *,
    choices: Iterable[str] | None = None,
    default: str | None = None,
) -> str:
    """Ask until the operator gives an acceptable answer.

    Args:
        question: Question text. Menu prompts keep a trailing `?` or `:` and
            otherwise append a colon. Free-text prompts always append `: `.
        choices: Menu choices selected by a single key, without Enter.
            Mark a non-initial abbreviation with brackets, as in `re[m]ove`;
            the first letter is the abbreviation otherwise.
        default: Value returned for a blank answer. `''` makes blank a valid
            free-text answer without showing a default.

    Returns:
        The chosen word without brackets, or the free-text answer with its
        surrounding whitespace intact.

    Raises:
        ValueError: Two choices use the same abbreviation.
        _errors.OperationCanceled: The operator pressed Ctrl-C.
        _errors.PubanonError: Input ended or the terminal is unusable.
    """
    if choices is None:
        suffix = f' [{escape(default)}]' if default else ''
        prompt = f'{style(question)}{style(suffix, "choice")}: '
        while True:
            answer = _read(prompt)
            if answer:
                return answer
            if default is not None:
                return default
            output('A value is required.', kind='warning')
    parsed = _parse_choices(choices)
    rendered = ' / '.join(rendered for _word, _letter, rendered in parsed)
    suffix = f' [{default}]' if default else ''
    separator = '' if question.endswith(('?', ':')) else ':'
    prompt = f'{style(question + separator)} {style(rendered + suffix, "choice")}: '
    words = [word for word, _letter, _rendered in parsed]
    hint = 'Choose ' + (
        f'{words[0]} or {words[1]}.'
        if len(words) == _CHOICE_PAIR_SIZE
        else ', '.join(words[:-1]) + f', or {words[-1]}.'
    )
    while True:
        answer = _read(prompt, key=True).strip().casefold()
        if not answer and default is not None:
            return default
        for word, letter, _rendered in parsed:
            if answer == letter:
                return word
        output(hint, kind='warning')


def confirm(question: str, *, default: bool | None = None) -> bool:
    """Ask a yes-or-no question until the operator answers it.

    Args:
        question: Question text, shown before the `[y/n]` hint. A default is
            shown capitalized, as `[Y/n]` or `[y/N]`.
        default: Returned for a blank answer. Without one, blank asks again.

    Returns:
        `True` for the `y` key, `False` for `n`, ignoring case; no Enter
        needed.

    Raises:
        _errors.OperationCanceled: The operator pressed Ctrl-C.
        _errors.PubanonError: Input ended or the terminal is unusable.
    """
    hint = {None: '[y/n]', True: '[Y/n]', False: '[y/N]'}[default]
    prompt = f'{style(question)} {style(hint, "choice")} '
    while True:
        answer = _read(prompt, key=True).strip().casefold()
        if not answer and default is not None:
            return default
        if answer in _YES:
            return True
        if answer in _NO:
            return False
        output('Enter y or n.', kind='warning')
