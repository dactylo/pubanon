"""Read operator answers and escape text for display."""

import os
import re
import shlex
import sys
import unicodedata
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TextIO

from pubanon._errors import OperationCancelled, PubanonError, PublicationCancelled

if os.name == 'nt':
    import msvcrt

    _TERMINAL_ERRORS = (EOFError, OSError)
else:
    import readline
    import termios
    import tty

    _TERMINAL_ERRORS = (EOFError, OSError, termios.error)

_BYTE_ESCAPE_MAX = 0xFF
_CHOICE_PAIR_SIZE = 2
_COLOR_MODE: ContextVar[str] = ContextVar('color_mode', default='auto')
_GUIDANCE_IMPLICIT_PROJECT: ContextVar[str | None] = ContextVar(
    'guidance_implicit_project', default=None
)
_GUIDANCE_PROJECT: ContextVar[str | None] = ContextVar('guidance_project', default=None)
_GUIDANCE_WORKFLOW: ContextVar[bool] = ContextVar('guidance_workflow', default=False)
_NO = frozenset({'n', 'no'})
_SHORT_ESCAPE_MAX = 0xFFFF
_STYLES = {
    'plain': '',
    'note': '2',
    'label': '1',
    'heading': '1;36',
    'choice': '36',
    'success': '32',
    'warning': '33',
    'error': '1;31',
}
_YES = frozenset({'y', 'yes'})


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
    """Read a line or menu key, converting terminal failures to Pubanon errors."""
    if not sys.stdin.isatty():
        raise PubanonError(
            'interactive input is required',
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
        raise OperationCancelled('operation cancelled') from None
    except _TERMINAL_ERRORS as exc:
        raise PubanonError(
            'interactive input is required',
            recovery='rerun in an interactive terminal',
        ) from exc


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


def shell_command(*args: str) -> str | None:
    """Render a shell command only when visible text preserves its arguments."""
    command = shlex.join(args)
    visible = command.replace('\\', '')
    return command if escape(visible) == visible else None


def guidance(text: str) -> str:
    """Qualify command recommendations and omit redundant project selections.

    Preserve explicit selections for other projects. Remove a selection for the
    project already implied by the current directory.

    Use only for advice written by Pubanon, never file contents, command
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
            with_mode = f' with {mode.group().strip()}' if mode is not None else ''
            return (
                f'Choose project {escape(project)} from its registered directory, '
                f'then rerun the {action} action{with_mode}.'
            )
        return f'{command} {option}{rest}'

    return re.sub(
        r'(?P<command>(?<![\w/\\])pubanon (?:repo (?:create|verify|reveal)|'
        r'stage (?:push|status|clean)|watch (?:ci|package)|list|show|delete|init|status|prepare|'
        r'inspect|discard|publish|release|run)(?![\w/-]))'
        r'(?P<rest>(?:(?![`;\n,]| then | or | && ).)*)',
        _qualify,
        text,
    )


def style(text: str, kind: str = 'label', *, stream: TextIO | None = None) -> str:
    """Style terminal text according to the scoped color mode.

    Args:
        text: Already escaped display text.
        kind: One of the terminal palette roles `plain`, `note`, `label`,
            `heading`, `choice`, `success`, `warning`, or `error`.
        stream: Destination used for terminal detection; defaults to stdout.

    Returns:
        Styled text when enabled, otherwise plain text.
    """
    target = sys.stdout if stream is None else stream
    code = _STYLES[kind]
    mode = _COLOR_MODE.get()
    if not text or not code or mode == 'never':
        return text
    if mode == 'auto' and ('NO_COLOR' in os.environ or not target.isatty()):
        return text
    return f'\x1b[{code}m{text}\x1b[0m'


def diagnostic(error: PubanonError, *, stream: TextIO | None = None) -> str:
    """Format an error or cancellation for terminal display.

    Args:
        error: Structured failure with already escaped details.
        stream: Destination used for terminal detection; defaults to stdout.

    Returns:
        A colored headline and indented, labeled recovery details.
    """
    if isinstance(error, PublicationCancelled):
        return '\n'.join(
            (
                style(str(error), 'warning', stream=stream),
                error.fact or '',
                '',
                f'{style("Next:", stream=stream)} {guidance(error.recovery or "pubanon publish")}',
            )
        )
    cancelled = isinstance(error, OperationCancelled)
    label = 'cancelled' if cancelled else 'error'
    kind = 'warning' if cancelled else 'error'
    lines = [style(f'{label}: {error}', kind, stream=stream)]
    for name, value in (
        ('fact', error.fact),
        ('expected', error.expected),
        ('actual', error.actual),
        ('recovery', error.recovery),
    ):
        if value is not None:
            rendered = guidance(value) if name == 'recovery' else value
            lines.append(f'  {style(name + ":", stream=stream)} {rendered}')
    return '\n'.join(lines)


def field(label: str, value: str, *, kind: str = 'plain') -> str:
    """Render a bold field label and optionally styled value.

    Args:
        label: Already escaped field name, without a trailing colon.
        value: Already escaped field value.
        kind: Presentation role for the value, using the roles accepted by
            `style`.
    """
    return f'{style(label + ":")} {style(value, kind)}'


def heading(text: str, *, separate: bool = False) -> None:
    """Print a bold title, optionally separated from preceding content.

    Args:
        text: Already escaped section title.
        separate: Add a blank line before an existing section.
    """
    print(('\n' if separate else '') + style(text.removesuffix(':'), 'heading'))


def next_step(text: str) -> None:
    """Print the next command unless an enclosing workflow owns the next step."""
    if not text or _GUIDANCE_WORKFLOW.get():
        return
    print(f'{style("Next:")} {style(guidance(text), "choice")}')


def output(text: str = '', *, kind: str = 'plain') -> None:
    """Print one terminal message using its presentation role.

    Args:
        text: Already escaped display text.
        kind: Presentation role accepted by `style`.
    """
    print(style(text, kind))


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
        OperationCancelled: The operator pressed Ctrl-C.
        PubanonError: Input ended or the terminal is unusable.
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
            if answer in {letter, word.casefold()}:
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
        OperationCancelled: The operator pressed Ctrl-C.
        PubanonError: Input ended or the terminal is unusable.
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
