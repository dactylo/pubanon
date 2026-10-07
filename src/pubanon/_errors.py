"""Render failures with the facts needed for safe recovery."""

from typing import Literal

from pubanon import _terminal


class ObservationTimeoutError(Exception):
    """Carry an unrendered observation-timeout signal between adapters.

    Attributes:
        stage: Optional observation stage that exceeded its time budget.
    """

    def __init__(
        self,
        *,
        stage: str | None = None,
    ) -> None:
        """Create an observation timeout with optional stage context.

        Args:
            stage: Optional observation stage that exceeded its time budget.
        """
        super().__init__()
        self.stage = stage


class PubanonError(Exception):
    """Describe a fail-closed pubanon error.

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
        with _terminal.color_context('never'):
            return _terminal.diagnostic(self)


class OperationCanceled(PubanonError):  # noqa: N818
    """Report operator interruption while preserving structured recovery facts.

    The CLI returns status 130 for this error. Diagnostic fields have the same
    contract as `PubanonError`.

    Attributes:
        diagnostic_kind: Presentation role for the known outcome, independent
            of interruption status: warning for cancellation, error for an
            unresolved problem, or success for a completed operation.
    """

    diagnostic_kind: Literal['warning', 'error', 'success'] = 'warning'


class PublicationCanceled(OperationCanceled):
    """Report publication cancelled before any push was attempted."""
