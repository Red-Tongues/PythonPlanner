"""Domain exceptions raised by the kernel."""

from __future__ import annotations


class OrchestratorError(Exception):
    """Base class for all kernel errors."""


class ImmutableRecordError(OrchestratorError):
    """Raised before any attempt to change a locked / accepted / append-only record."""


class InvalidTransitionError(OrchestratorError):
    """Raised when a packet status change is not in the allowed-transition table."""


class UnknownResponseStructureError(OrchestratorError, LookupError):
    """Raised when a response-structure name has no registered Pydantic contract."""


class NotFoundError(OrchestratorError, LookupError):
    """Raised when a record looked up by its human-readable ref does not exist."""


class IngestionError(OrchestratorError):
    """Raised when a document cannot be turned into a ProjectTree at all."""


class AlreadySupersededError(OrchestratorError):
    """Raised when superseding a packet that already has a newer version (history is linear)."""


class UnauditedWriteError(OrchestratorError):
    """Raised when a domain row would change without an event in the same flush (R1 fix 7)."""
