"""obs-websocket 5 client protocol (RPC version 1), independent of the transport.

Checked against obs-websocket 5.6.3, the version bundled with OBS Studio
32.0.x: `docs/generated/protocol.md` and `protocol.json` at tag 5.6.3, and
the server sources it describes (`src/utils/Crypto.cpp` for authentication,
`src/requesthandler/rpc/Request.cpp` for how a target is resolved).

`ObsConnection` speaks the protocol over a `Channel`, which carries whole
text messages; opening one (TCP, the WebSocket handshake, framing) is the
transport's job. One reader receives everything OBS sends. It answers each
request's own waiter and hands every event to the provider, so events keep
flowing while requests are outstanding. Nothing here logs, and no OBS text
(comments, close reasons) is kept or passed on.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED = 0, 1, 2
OP_EVENT, OP_REQUEST, OP_RESPONSE = 5, 6, 7
RPC_VERSION = 1
# EventSubscription bits: General | Config | Scenes | Inputs. High-volume
# events (meters, show/active state) need their own bits and stay off.
EVENT_SUBSCRIPTIONS = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3)

UUID_VERSION = (5, 4, 0)       # scene and input UUIDs (OBS Studio 30.1 and later)
CAPS_VERSION = (5, 6, 0)       # inputKindCaps in GetInputList (OBS Studio 31.1 and later)

# WebSocketCloseCode
CLOSE_AUTHENTICATION_FAILED = 4009
CLOSE_UNSUPPORTED_RPC_VERSION = 4010
CLOSE_SESSION_INVALIDATED = 4011

# RequestStatus
STATUS_SUCCESS = 100
STATUS_NOT_READY = 207
STATUS_INVALID_RESOURCE_TYPE = 602
STATUS_RESOURCE_NOT_FOUND = 600
STATUS_INVALID_RESOURCE_STATE = 604

MESSAGE_MAX_BYTES = 128 * 1024         # host complete-message UTF-8 limit
MESSAGE_MAX_CHARS = MESSAGE_MAX_BYTES  # compatibility alias for existing tests
PENDING_MAX = 32                        # requests waiting for an answer at once


class Channel(Protocol):
    """An open connection to OBS that carries whole text messages.

    `receive` returns None once the connection has closed; `close_code` is
    then the WebSocket close code OBS sent, or None. `send` raises
    `ConnectionError` if the message could not be handed to the connection.
    """

    close_code: int | None

    async def receive(self) -> str | None: ...

    async def send(self, text: str) -> None: ...

    async def close(self) -> None: ...


Connector = Callable[[str, int], Awaitable[Channel]]


class ProtocolFailure(Exception):
    """OBS sent something this client does not accept."""


class RequiredFeaturesMissing(ProtocolFailure):
    """The peer lacks the UUID/audio features this package requires."""


class Closed(Exception):
    """The connection is closed.

    `code` is OBS's close code, if any. `sent` is True when the request that
    raised this had already been handed to the connection, so OBS may have
    acted on it.
    """

    def __init__(self, code: int | None, *, sent: bool = False):
        super().__init__("closed")
        self.code = code
        self.sent = sent


class Busy(Exception):
    """Too many requests are already waiting for OBS; nothing was sent."""


class RequestFailed(Exception):
    """OBS answered with a failure status. Its comment is never kept."""

    def __init__(self, status: int):
        super().__init__(str(status))
        self.status = status


@dataclass(frozen=True)
class Hello:
    websocket_version: tuple[int, int, int]
    authentication: dict | None        # {"challenge": str, "salt": str} when a password is needed


def authentication_string(password: str, salt: str, challenge: str) -> str:
    """The `Identify` authentication answer (obs-websocket `Utils::Crypto`).

    secret = base64(sha256(password + salt)); answer =
    base64(sha256(secret + challenge)), all text UTF-8 encoded.
    """
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    answer = hashlib.sha256(secret + challenge.encode("utf-8")).digest()
    return base64.b64encode(answer).decode("ascii")


def parse_version(text: Any) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})", text) if isinstance(text, str) else None
    return None if match is None else tuple(int(part) for part in match.groups())


def _object_without_duplicates(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolFailure()
        result[key] = value
    return result


def _reject_constant(_name: str) -> None:
    raise ProtocolFailure()


def decode(text: Any) -> tuple[int, dict]:
    """(op, d) of one OBS message, or ProtocolFailure."""
    if not isinstance(text, str) or len(text) > MESSAGE_MAX_BYTES:
        raise ProtocolFailure()
    try:
        if len(text.encode("utf-8")) > MESSAGE_MAX_BYTES:
            raise ProtocolFailure()
    except UnicodeError:
        raise ProtocolFailure() from None
    try:
        message = json.loads(text, object_pairs_hook=_object_without_duplicates,
                             parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise ProtocolFailure() from None
    if not isinstance(message, dict):
        raise ProtocolFailure()
    op, data = message.get("op"), message.get("d")
    if type(op) is not int or not isinstance(data, dict):
        raise ProtocolFailure()
    return op, data


class ObsConnection:
    """One identified obs-websocket session over `channel`.

    `on_event(event_type, event_data)` runs on the reader for every event, in
    the order OBS sent them; it must not wait. It may raise ProtocolFailure
    for malformed event data, which ends the connection.
    """

    def __init__(self, channel: Channel, on_event: Callable[[str, dict], None]):
        self._channel = channel
        self._on_event = on_event
        self._pending: dict[str, asyncio.Future] = {}
        self._number = 0                          # request IDs are m-1, m-2, ...
        self._closed: Closed | None = None
        self.identified = False
        self.failed = False                       # this client ended it: OBS broke the protocol
        self.late_answers = 0

    @property
    def closed(self) -> bool:
        return self._closed is not None

    async def handshake(self, password: Callable[[], Awaitable[str]]) -> Hello:
        """Receive Hello, identify (signing in if OBS asks), wait for Identified."""
        op, data = await self._receive()
        if op != OP_HELLO:
            raise ProtocolFailure()
        version = parse_version(data.get("obsWebSocketVersion"))
        rpc = data.get("rpcVersion")
        auth = data.get("authentication")
        if version is None or type(rpc) is not int or rpc < RPC_VERSION:
            raise ProtocolFailure()
        if auth is not None and not (isinstance(auth, dict)
                                     and isinstance(auth.get("challenge"), str)
                                     and isinstance(auth.get("salt"), str)):
            raise ProtocolFailure()
        identify = {"rpcVersion": RPC_VERSION, "eventSubscriptions": EVENT_SUBSCRIPTIONS}
        if auth is not None:
            identify["authentication"] = authentication_string(
                await password(), auth["salt"], auth["challenge"])
        await self._send({"op": OP_IDENTIFY, "d": identify}, sent=False)
        op, data = await self._receive()
        if op != OP_IDENTIFIED or data.get("negotiatedRpcVersion") != RPC_VERSION:
            raise ProtocolFailure()
        self.identified = True
        return Hello(version, auth)

    async def run(self) -> Closed:
        """Receive until the connection ends; returns why it ended.

        Every request still waiting then fails with `Closed(sent=True)`.
        """
        reason = Closed(None)
        try:
            while True:
                op, data = await self._receive()
                if op == OP_RESPONSE:
                    self._answer(data)
                elif op == OP_EVENT:
                    event_type, event_data = data.get("eventType"), data.get("eventData", {})
                    if not isinstance(event_type, str) or not isinstance(event_data, dict):
                        raise ProtocolFailure()
                    self._on_event(event_type, event_data)
                else:
                    raise ProtocolFailure()
        except Closed as closed:
            reason = closed
        except ProtocolFailure:
            self.failed = True
            await self.close()
        finally:
            self._end(reason)
        return reason

    async def request(self, request_type: str, data: dict | None = None) -> dict:
        """`responseData` of a successful request (an empty dict if none).

        Raises `RequestFailed`, `Closed` (with `sent` telling whether OBS may
        have received it) or `Busy`. A cancelled waiter abandons the request;
        its late answer is ignored.
        """
        if self._closed is not None or not self.identified:
            raise Closed(None if self._closed is None else self._closed.code)
        if len(self._pending) >= PENDING_MAX:
            raise Busy()
        self._number += 1
        request_id = f"m-{self._number}"
        body = {"requestType": request_type, "requestId": request_id}
        if data:
            body["requestData"] = data
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"op": OP_REQUEST, "d": body}, sent=True)
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def close(self) -> None:
        try:
            await self._channel.close()
        except Exception:
            pass

    # ── internals ──

    async def _receive(self) -> tuple[int, dict]:
        text = await self._channel.receive()
        if text is None:
            raise Closed(self._channel.close_code)
        return decode(text)

    async def _send(self, message: dict, *, sent: bool) -> None:
        if self._closed is not None:
            raise Closed(self._closed.code)
        try:
            text = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
            if len(text.encode("utf-8")) > MESSAGE_MAX_BYTES:
                raise ProtocolFailure()
            await self._channel.send(text)
        except ConnectionError:
            # Part of the message may have reached OBS: a request is uncertain.
            raise Closed(self._channel.close_code, sent=sent) from None

    def _answer(self, data: dict) -> None:
        request_id, status = data.get("requestId"), data.get("requestStatus")
        response = data.get("responseData", {})
        if not isinstance(request_id, str) or not isinstance(status, dict) \
                or not isinstance(response, dict):
            raise ProtocolFailure()
        result, code = status.get("result"), status.get("code")
        if type(result) is not bool or type(code) is not int:
            raise ProtocolFailure()
        future = self._pending.pop(request_id, None)
        if future is None:
            # An abandoned (timed-out) request's answer is ignored; an answer
            # to a request this connection never sent ends the connection.
            match = re.fullmatch(r"m-([1-9][0-9]{0,17})", request_id)
            if match is None or int(match.group(1)) > self._number:
                raise ProtocolFailure()
            self.late_answers += 1
            return
        if future.done():
            return
        if result and code == STATUS_SUCCESS:
            future.set_result(response)
        else:
            future.set_exception(RequestFailed(code))

    def _end(self, reason: Closed) -> None:
        if self._closed is None:
            self._closed = reason
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_exception(Closed(reason.code, sent=True))
                future.exception()                   # retrieved: its waiter may be gone
        self._pending.clear()
