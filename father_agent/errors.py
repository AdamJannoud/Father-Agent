"""Exception hierarchy for the Father Agent.

Every failure the factory can surface to a user is one of these, so the CLI
can print a clear one-line message instead of a traceback.
"""

from __future__ import annotations


class FatherAgentError(Exception):
    """Base class for every error raised by the Father Agent."""


class ConfigError(FatherAgentError):
    """Raised when configuration is invalid (for example a non-free endpoint)."""


class ProviderError(FatherAgentError):
    """Raised when a model provider call fails."""

    def __init__(self, message: str, *, provider: str = "", status: int | None = None,
                 retryable: bool = False, retry_after: float | None = None) -> None:
        """Store the provider name, HTTP status and whether a retry may help."""
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class AllProvidersFailedError(ProviderError):
    """Raised when every provider in the chain has failed for one request."""


class SpecError(FatherAgentError):
    """Raised when the planner cannot produce a spec that satisfies the contract."""


class ValidationFailedError(FatherAgentError):
    """Raised when a generated file fails the validator gate and cannot be repaired."""

    def __init__(self, message: str, *, filename: str = "",
                 problems: list[str] | None = None) -> None:
        """Record which file failed and the list of problems the gate found."""
        super().__init__(message)
        self.filename = filename
        self.problems = problems or []


class OutputExistsError(FatherAgentError):
    """Raised when the target sub-agent folder already exists and --force was not given."""
