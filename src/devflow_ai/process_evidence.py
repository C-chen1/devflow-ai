"""Evidence about whether a managed process was ever started.

The difference between "the command failed" and "the command may have run anyway" is
not decidable from an exception type: a ``ValueError`` raised while validating a
command proves nothing about a process that already exists. Only the code that
creates the process can state that no process was created, so that code marks the
error it raised and everything else stays unknown. Marking is deliberately one-way:
an unmarked error always means "no evidence", never "not started".
"""
from __future__ import annotations

from typing import TypeVar

_NOT_STARTED_ATTRIBUTE = "devflow_process_not_started"

_Error = TypeVar("_Error", bound=BaseException)


def mark_process_not_started(error: _Error) -> _Error:
    """Attach proof that the reporting boundary created no process.

    Called by the process-creation boundary only, at the point where a failure
    demonstrably happened before any child existed. The original exception object is
    returned so callers can ``raise mark_process_not_started(error)`` and keep both
    the type and the message other handlers already rely on.
    """
    try:
        setattr(error, _NOT_STARTED_ATTRIBUTE, True)
    except (AttributeError, TypeError):
        # An exception that refuses attributes simply carries no proof.
        pass
    return error


def proves_process_not_started(error: BaseException) -> bool:
    """True only when the boundary recorded that no process was started."""
    return getattr(error, _NOT_STARTED_ATTRIBUTE, False) is True
