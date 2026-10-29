"""Calls from the integration to MIDImap host services, and background work.

Every host call carries the ID of the host request it serves
(`parent_request_id`), taken automatically from the request being handled,
never from whichever request arrived last. Its deadline is the smaller of
the requested timeout, 30 seconds, and the time left on that request. Work
that is not part of a request, or that must outlive it, is started with
`Background.start()`; its host calls are sent as background calls and are
only accepted while the instance is Ready. A task that inherits a request's
context and keeps running after that request finished cannot make host
calls; it raises `ContextError` instead of silently borrowing a finished
request's identity.

Host calls are not permission grants: MIDImap checks the instance's grants
for every call, whatever the integration believes.
"""
from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .errors import WebSocketClosed

if TYPE_CHECKING:
    from .runtime import WorkerRuntime


class CallContext:
    """The request (or background work) the current task is serving."""

    __slots__ = ("request_id", "deadline", "finished")

    def __init__(self, request_id: str | None, deadline: float | None):
        self.request_id = request_id          # None for background work
        self.deadline = deadline              # monotonic seconds; None for background
        self.finished = False


current_call: contextvars.ContextVar[CallContext | None] = contextvars.ContextVar(
    "midimap_integration_call", default=None)


@dataclass(frozen=True)
class WebSessionInfo:
    """Non-secret status of a host-run browser web session, from
    `Setup.host.web_session_info`.

    `connected` is whether a usable session is stored; `account` is the signed-in
    account's display label and `structure_id` / `structure_name` the selected
    home, each None until connected. `epoch` changes whenever the stored session
    is replaced, so background work can notice a reconnection. No cookie,
    credential or header is ever exposed.
    """
    connected: bool
    account: str | None
    structure_id: str | None
    structure_name: str | None
    epoch: int
    renewal: str = "unavailable"
    last_rotation: int | None = None
    last_success: int | None = None
    last_failure: int | None = None
    failure_kind: str = ""


@dataclass(frozen=True, repr=False)
class WebSessionResponse:
    """One web-session operation's result, from `Setup.host.web_session_request`.

    `status` is the provider's HTTP status; `data` is the parsed response body
    (plain JSON), never headers or credentials. A `status` of 200 means the
    provider accepted the request, not that any device physically changed.
    `data` is never logged by MIDImap, so neither is this.
    """
    status: int
    data: object

    def __repr__(self) -> str:
        return f"WebSessionResponse(status={self.status}, data=<parsed>)"


@dataclass(frozen=True, repr=False)
class AccessToken:
    """The current access token of one sign-in, from `Setup.host.access_token`.

    `expires_at` is epoch seconds, or None when the provider did not say.
    Never log `value` or put it in results, errors, events, labels or state.
    """
    value: str
    expires_at: int | None
    scopes: tuple[str, ...]

    def __repr__(self) -> str:
        return f"AccessToken(<redacted>, expires_at={self.expires_at!r}, scopes={self.scopes!r})"

    __str__ = __repr__


class HostServices:
    """Host-service client available as `Setup.host`."""

    def __init__(self, runtime: WorkerRuntime):
        self._runtime = runtime

    async def probe(self, permission_id: str, *, timeout_s: float = 5.0) -> bool:
        """Test-harness service: whether the synthetic grant is active.

        Raises `HostCallError` if the host refuses the call (for example
        `PERMISSION_DENIED`), `HostCallTimeout` if no answer arrives in time.
        """
        result = await self._runtime.host_call(
            "host.test.probe", {"permission_id": permission_id}, timeout_s)
        return result["allowed"]

    async def read_secret(self, field_id: str, *, timeout_s: float = 5.0) -> str:
        """The saved value of one declared secret field.

        Needs the field's granted `secret.read` permission; from an action,
        that action must also list the permission. MIDImap checks this on
        every call. Raises `HostCallError` with PERMISSION_DENIED (not
        granted), NEEDS_SETUP (nothing saved, or unreadable) or
        INSTANCE_UNAVAILABLE, or `HostCallTimeout`.

        Once returned, the value is in this integration's hands: never log it,
        put it in results, errors, events, labels or state, or store it
        outside memory. MIDImap cannot filter what an integration does with it.
        """
        result = await self._runtime.host_call(
            "host.secret.read", {"field_id": field_id}, timeout_s)
        return result["value"]

    async def access_token(self, auth_id: str, *, timeout_s: float = 5.0) -> AccessToken:
        """The current access token of one declared sign-in entry.

        Needs the entry's granted `auth.token` permission; from an action,
        that action must also list it. MIDImap signs in, stores and renews
        the token; the integration never sees a refresh token or code.
        Raises `HostCallError` with PERMISSION_DENIED, NEEDS_SETUP (not
        signed in, expired, or the sign-in could not be renewed safely),
        INSTANCE_UNAVAILABLE (often retryable: try again later) or
        SATURATED, or `HostCallTimeout`. A renewal may take a few seconds;
        give the call enough `timeout_s`.

        Once returned, the token is in this integration's hands: never log
        it or put it in results, errors, events, labels or state.
        """
        result = await self._runtime.host_call(
            "host.auth.token", {"auth_id": auth_id}, timeout_s)
        return AccessToken(result["access_token"], result["expires_at"],
                           tuple(result["scopes"]))

    async def open_websocket(self, connection_id: str, *, timeout_s: float = 10.0) -> WebSocket:
        """Open the package's declared connection; MIDImap owns the socket.

        Needs the connection's granted `network.websocket` permission for
        exactly the endpoint the instance's settings select; from an action,
        that action must list it. One connection may be open at a time.
        Raises `HostCallError` with PERMISSION_DENIED (not granted, or the
        endpoint resolves to an address it may not use), INSTANCE_UNAVAILABLE
        (unreachable, refused, certificate not verified; often retryable),
        INVALID_ARGUMENT (already open) or SATURATED, or `HostCallTimeout`.
        """
        result = await self._runtime.host_call(
            "host.websocket.open", {"connection_id": connection_id}, timeout_s)
        return WebSocket(self._runtime, result["handle"])

    async def web_session_info(self, session_id: str, *,
                               timeout_s: float = 10.0) -> WebSessionInfo:
        """Non-secret status of the package's declared host-run web session.

        Needs the session's granted `network.web_session` permission; from an
        action, that action must also list it. MIDImap owns the browser profile,
        cookies, signing and HTTPS; this returns connection status, the account
        label, the selected home and a session epoch. Renewal health is kept
        by the host and is not part of the version 1 wire response. Raises
        `HostCallError` (PERMISSION_DENIED, NEEDS_SETUP when no session is
        connected, INSTANCE_UNAVAILABLE, SATURATED) or `HostCallTimeout`.
        """
        result = await self._runtime.host_call(
            "host.web_session.info", {"session_id": session_id}, timeout_s)
        return WebSessionInfo(
            result["connected"], result["account"], result["structure_id"],
            result["structure_name"], result["epoch"], result.get("renewal", "unavailable"),
            result.get("last_rotation"), result.get("last_success"),
            result.get("last_failure"), result.get("failure_kind", ""))

    async def web_session_request(self, session_id: str, operation: str, body: object, *,
                                  timeout_s: float = 10.0) -> WebSessionResponse:
        """Run one fixed `operation` of the web session and return its response.

        The host maps `operation` to a fixed provider path, signs the request
        with the stored session and forwards `body` as the request body; the
        worker never supplies a path, headers or credentials. The allowed
        operations are the session profile's; a write operation runs only from
        an action's context. Needs the session's granted `network.web_session`
        permission (and, from an action, that the action lists it). Raises
        `HostCallError` (PERMISSION_DENIED, NEEDS_SETUP, INVALID_ARGUMENT for an
        unknown operation, INSTANCE_UNAVAILABLE, SATURATED) or `HostCallTimeout`.
        A returned response is acceptance, not physical confirmation.
        """
        result = await self._runtime.host_call(
            "host.web_session.request",
            {"session_id": session_id, "operation": operation, "body": body}, timeout_s)
        return WebSessionResponse(result["status"], result["data"])

    def remaining_s(self) -> float | None:
        """Seconds left before the current request's deadline (None in background)."""
        return self._runtime.remaining_s()


class WebSocket:
    """One open connection from `Setup.host.open_websocket`.

    `send` hands one text message to MIDImap's connection. Success means it
    was submitted to the connection, not that the peer acted on it. A
    `HostCallError` with `outcome_unknown` means it may have been delivered;
    never resend it automatically.

    `receive` returns the next text message, in order, or raises
    `WebSocketClosed` once the connection has ended (every message received
    before that is returned first). Its host calls are background calls, so
    a reader started during initialization keeps working after it. If a
    `receive` is cancelled, the call it made keeps running and the next
    `receive` returns its message: none is lost or returned twice. Only one
    receive may wait at a time.
    """

    def __init__(self, runtime: WorkerRuntime, handle: str):
        self._runtime = runtime
        self._handle = handle
        self._pending: asyncio.Task | None = None     # the receive call in flight
        self._receiving = False                       # a caller is waiting in receive()
        self.close_code: int | None = None

    def __repr__(self) -> str:
        return f"WebSocket(close_code={self.close_code!r})"

    async def send(self, text: str, *, timeout_s: float = 10.0) -> None:
        await self._runtime.host_call(
            "host.websocket.send", {"handle": self._handle, "text": text}, timeout_s)

    async def receive(self) -> str:
        if self._receiving:
            raise RuntimeError("another receive is already waiting on this connection")
        self._receiving = True
        try:
            return await self._receive()
        finally:
            self._receiving = False

    async def _receive(self) -> str:
        while True:
            if self.close_code is not None:
                raise WebSocketClosed(self.close_code)
            if self._pending is None:
                self._pending = self._runtime.own(self._runtime.host_call(
                    "host.websocket.receive", {"handle": self._handle}, 30.0, detached=True))
            pending = self._pending
            try:
                result = await asyncio.shield(pending)
            except asyncio.CancelledError:
                if pending.cancelled() and self._pending is pending:
                    self._pending = None        # the worker is stopping: the call is gone
                raise
            except BaseException:
                if self._pending is pending:
                    self._pending = None
                raise
            if self._pending is pending:
                self._pending = None
            if result["kind"] == "message":
                return result["text"]
            if result["kind"] == "closed":
                self.close_code = result["code"]
            # "wait": nothing arrived in the host's bounded wait; ask again.

    async def close(self, *, timeout_s: float = 10.0) -> None:
        """Close the connection (idempotent); queued messages are discarded."""
        await self._runtime.host_call(
            "host.websocket.close", {"handle": self._handle}, timeout_s)


class Background:
    """Owned background tasks, available as `Setup.background`."""

    def __init__(self, runtime: WorkerRuntime):
        self._runtime = runtime

    def start(self, work: Callable[[], Awaitable[Any]], *, name: str = "background") -> None:
        """Run `work()` as background work owned by the worker.

        At most 8 run at once; starting another raises
        `IntegrationError(SATURATED)`. They are cancelled when the worker
        shuts down. Exceptions are logged by code only.
        """
        self._runtime.start_background(work, name)
