"""Render failures with the facts needed for safe recovery."""


class ObservationTimeoutError(Exception):
    """Carry an unrendered observation-timeout signal between adapters.

    Attributes:
        stage: Optional observation stage that exceeded its time budget.
        stdout: Raw standard output retained from the timed-out child.
        stderr: Raw standard error retained from the timed-out child.
    """

    def __init__(
        self,
        *,
        detail: str | None = None,
        stage: str | None = None,
        stdout: bytes = b'',
        stderr: bytes = b'',
    ) -> None:
        """Create an observation timeout with retained raw context.

        Args:
            detail: Optional safe diagnostic derived from timed-out output.
            stage: Optional observation stage that exceeded its time budget.
            stdout: Raw standard output retained from the timed-out child.
            stderr: Raw standard error retained from the timed-out child.
        """
        super().__init__()
        self._detail = detail
        self.stage = stage
        self.stderr = stderr
        self.stdout = stdout


class PubanonError(Exception):
    """Describe a fail-closed Pubanon error.

    Attributes:
        actual: Optional observed state that caused the refusal.
        expected: Optional state required for the operation to proceed.
        fact: Optional supporting context for the refusal.
        recovery: Optional operator action that can make a retry safe.
    """

    def __init__(
        self,
        message: str,
        *,
        fact: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
        recovery: str | None = None,
    ) -> None:
        """Create a fail-closed error with optional recovery context.

        Args:
            message: Primary refusal message.
            fact: Optional supporting context for the refusal.
            expected: Optional state required for the operation to proceed.
            actual: Optional observed state that caused the refusal.
            recovery: Optional operator action that can make a retry safe.
        """
        super().__init__(message)
        self.actual = actual
        self.expected = expected
        self.fact = fact
        self.recovery = recovery

    def render(self) -> str:
        """Render the error for display to the operator.

        Returns:
            The message and diagnostic details.
        """
        lines = [f'error: {self}']
        for label, value in (
            ('fact', self.fact),
            ('expected', self.expected),
            ('actual', self.actual),
            ('recovery', self.recovery),
        ):
            if value is not None:
                lines.append(f'{label}: {value}')
        return '\n'.join(lines)


class OperationCancelled(PubanonError):  # noqa: N818
    """Report operator interruption while preserving structured recovery facts.

    The CLI returns status 130 for this error. Diagnostic fields have the same
    contract as `PubanonError`.
    """


class PublicationCancelled(OperationCancelled):
    """Report publication cancelled before any push was attempted."""
