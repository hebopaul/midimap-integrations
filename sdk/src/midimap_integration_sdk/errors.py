"""Errors an integration raises and receives.

Raise `IntegrationError` for every failure the user should see. Its message
is shown in MIDImap and may be written to diagnostics, so it must be a fixed
sentence you wrote: never provider response text, exception text,
credentials, configuration or action arguments. Any other exception from your
code is reported as `PROVIDER_ERROR` with a fixed message, and its text is
never sent to the host or written to the log.
"""
from __future__ import annotations

from ._wire.contracts import HOST_ONLY_ERROR_CODES, DomainError, ErrorCode, ErrorInfo

GENERIC_FAILURE = "The integration failed."


class IntegrationError(Exception):
    """A safe failure reported to the host as an error response.

    `code` is an `ErrorCode` a worker may report (host-only codes such as
    `STORE_ERROR` are refused). `message` is at most 512 characters with no
    control characters. `retryable` hints that an explicit user retry may
    help; it never makes MIDImap replay an action. Set `outcome_unknown` when
    an external effect may already have happened.
    """

    def __init__(self, code: ErrorCode | str, message: str, *, retryable: bool = False,
                 outcome_unknown: bool = False):
        try:
            code = ErrorCode(code)
        except ValueError:
            raise ValueError("unknown integration error code") from None
        if code in HOST_ONLY_ERROR_CODES:
            raise ValueError("this error code is reserved for the MIDImap host")
        try:
            self.info = ErrorInfo(code, message, retryable, outcome_unknown)
        except DomainError as error:
            raise ValueError(f"invalid integration error {error.pointer}: "
                             f"{error.message}") from None
        super().__init__(code.value)        # str() never repeats the message

    @property
    def code(self) -> ErrorCode:
        return self.info.code

    @property
    def message(self) -> str:
        return self.info.message


class HostCallError(IntegrationError):
    """The host answered a host-service call with an error."""

    @classmethod
    def from_info(cls, info: ErrorInfo) -> HostCallError:
        error = cls.__new__(cls)
        error.info = info
        Exception.__init__(error, info.code.value)
        return error


class HostCallTimeout(IntegrationError):
    """No answer arrived within the call's deadline.

    The host may still be working on the call; this only means the wait ended.
    """

    def __init__(self):
        super().__init__(ErrorCode.TIMEOUT, "The host did not answer in time.",
                         outcome_unknown=True)


class WebSocketClosed(Exception):
    """The connection ended. `code` is its numeric close code (OBS's own, or
    1006 when it ended without one); no remote text is ever passed on."""

    def __init__(self, code: int):
        super().__init__(f"connection closed ({code})")
        self.code = code


class NotReadyError(IntegrationError):
    """The worker is not accepting this work now (not initialized, or stopping)."""

    def __init__(self):
        super().__init__(ErrorCode.INSTANCE_UNAVAILABLE, "The integration is not ready.")


class EventQueueFull(IntegrationError):
    """Too many events are waiting to be sent; nothing was queued."""

    def __init__(self):
        super().__init__(ErrorCode.SATURATED, "Too many events are waiting to be sent.")


class TooManyObjects(IntegrationError):
    """A new object would exceed the objects a worker session may report.

    Objects already reported in the session stay updatable; nothing was
    queued or sent.
    """

    def __init__(self):
        super().__init__(ErrorCode.SATURATED,
                         "The integration reported more objects than MIDImap keeps track of.")


class ContextError(RuntimeError):
    """A host call was made outside the request or background work it needs.

    Host calls made while handling a request are linked to that request
    automatically. Work that outlives its request, or runs on its own, must be
    started with `Setup.background.start()`.
    """


def safe_error(error: BaseException) -> ErrorInfo:
    """The error reported for an exception raised by integration code.

    A `HostCallError` re-raised as is may carry a host-only code, which a
    worker may not report; it becomes the generic failure like any other.
    """
    if isinstance(error, IntegrationError) and error.code not in HOST_ONLY_ERROR_CODES:
        return error.info
    return ErrorInfo(ErrorCode.PROVIDER_ERROR, GENERIC_FAILURE)
