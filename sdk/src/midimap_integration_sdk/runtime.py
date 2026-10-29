"""Worker runtime: speaks the MIDImap worker protocol for one `Provider`.

One reader task decodes host frames and dispatches each request to its own
task without awaiting it, so a slow action never blocks control requests or
host-call replies. One writer task serializes all output with a strict
priority: control responses (initialize, discovery, state, shutdown) and
host-call requests first, then action responses, then events. Every queue,
handler count, host call and background task is bounded by the shared
policy. Nothing starts until `run()` is awaited.

Completion semantics, matching the host engine:

* each host request gets exactly one terminal response, sent when its
  handler task has actually exited (not when an await timed out); its
  capacity is held until then;
* at the request deadline the handler is cancelled; if it still completes,
  its validated result is sent (the host keeps the timed-out outcome and
  uses the reply only as settlement);
* once shutdown starts, a handler that resists cancellation and returns a
  value is reported as CANCELLED, never as success;
* host-call replies are matched only against pending calls; a call keeps its
  slot until the host answers, even if the caller stopped waiting.

This is a trusted executable extension, not a sandbox: the SDK validates and
bounds its own protocol traffic, but cannot stop provider code from using
the operating system directly. The MIDImap host checks grants for every
host-service call regardless of what the SDK reports.
"""
from __future__ import annotations

import asyncio
import contextvars
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, Protocol

from ._wire import policy
from ._wire.contracts import (
    DiscoveryPage, DomainError, ErrorCode, ErrorInfo, Event, ObjectRecord, PackageManifest,
    Request, Response, StateRecord, check_unique, request_number, thaw_json,
)
from ._wire.framing import LineDecoder
from ._wire.validation import (
    WEBSOCKET_METHODS,
    decode_message, encode_message, parse_grant_set, parse_method_result, resolve_action,
    validate_arguments, validate_config, validate_discovery_page, validate_grants,
    validate_result, validate_state_values,
)
from .clock import LoopScheduler, Scheduler, TimerHandle
from .errors import (
    ContextError, HostCallError, HostCallTimeout, IntegrationError, NotReadyError, safe_error,
)
from .events import EventOutbox, EventPublisher, ObjectTracker, state_digest
from .host import Background, CallContext, HostServices, current_call
from .provider import Grants, Page, Provider, Setup, State, action_handlers

log = logging.getLogger("midimap_integration_sdk")


class Transport(Protocol):
    """The worker's protocol input (host → worker) and output (worker → host)."""

    async def read(self, max_bytes: int) -> bytes:
        """Up to `max_bytes`; `b""` once the host closed the stream."""

    async def write(self, data: bytes) -> None:
        """Return once `data` has been handed to the stream; raise if broken."""


Spawn = Callable[[Coroutine[Any, Any, Any], contextvars.Context], "asyncio.Task[Any]"]


class ExitCode(IntEnum):
    CLEAN = 0              # shutdown answered after complete cleanup
    UNCLEAN = 1            # cleanup incomplete, host closed the stream, or write failed
    PROTOCOL_VIOLATION = 2  # the host sent something the protocol does not allow


class WorkerState(StrEnum):
    NEW = "new"
    INITIALIZING = "initializing"
    READY = "ready"
    STOPPING = "stopping"
    CLOSED = "closed"


@dataclass(frozen=True)
class WorkerLimits:
    action_handlers: int = policy.ACTION_OPERATIONS_PER_INSTANCE_MAX
    control_handlers: int = policy.HOST_CONTROL_REQUESTS_MAX
    host_calls: int = policy.WORKER_HOST_OPERATIONS_MAX
    background_tasks: int = policy.WORKER_BACKGROUND_TASKS_MAX
    event_queue: int = policy.EVENT_QUEUE_MAX
    tracked_objects: int = policy.CACHED_OBJECTS_MAX
    write_queue: int = policy.WRITE_QUEUE_MAX
    read_chunk_bytes: int = policy.STREAM_READ_CHUNK_BYTES
    shutdown_reply_margin_s: float = policy.WORKER_SHUTDOWN_REPLY_MARGIN_S
    abort_cleanup_s: float = policy.SHUTDOWN_GRACE_S


def _error(code: ErrorCode, message: str, *, outcome_unknown: bool = False) -> ErrorInfo:
    return ErrorInfo(code, message, outcome_unknown=outcome_unknown)


_INVALID_RESULT = "The integration returned an invalid result."
_TIMED_OUT = object()


class _Handler:
    __slots__ = ("request", "kind", "context", "task", "timer", "cancel_reason", "finish")

    def __init__(self, request: Request, kind: str, context: CallContext,
                 finish: Callable[[Any], Any]):
        self.request = request
        self.kind = kind                      # "lifecycle" | "control" | "action"
        self.context = context
        self.finish = finish                  # value -> wire result; DomainError if invalid
        self.task: asyncio.Task | None = None
        self.timer: TimerHandle | None = None
        self.cancel_reason: str | None = None  # "deadline" | "shutdown"


class _HostCall:
    __slots__ = ("id", "method", "deadline", "waiter", "timer")

    def __init__(self, request_id: str, method: str, deadline: float):
        self.id = request_id
        self.method = method
        self.deadline = deadline
        self.waiter: asyncio.Future | None = None
        self.timer: TimerHandle | None = None


async def _call(function: Callable, *args: Any) -> Any:
    """Run sync or async integration code inside the current task."""
    result = function(*args)
    if inspect.isawaitable(result):
        result = await result
    return result


class WorkerRuntime:
    """Runs `provider` against `manifest` over `transport` until shutdown."""

    def __init__(self, provider: Provider, manifest: PackageManifest, *, transport: Transport,
                 scheduler: Scheduler | None = None, limits: WorkerLimits = WorkerLimits(),
                 spawn: Spawn | None = None):
        if not isinstance(provider, Provider):
            raise TypeError("provider must be a midimap_integration_sdk.Provider")
        if not isinstance(manifest, PackageManifest):
            raise TypeError("manifest must come from load_manifest()")
        self._actions = action_handlers(provider)
        declared = {a.id for a in manifest.actions}
        if set(self._actions) != declared:
            missing = sorted(declared - set(self._actions))
            extra = sorted(set(self._actions) - declared)
            raise ValueError(f"action handlers do not match the manifest "
                             f"(missing: {missing}, undeclared: {extra})")
        self.provider = provider
        self.manifest = manifest
        self._transport = transport
        self._scheduler = scheduler
        self._limits = limits
        self._spawn_task = spawn
        self.outbox = EventOutbox(limits.event_queue)
        self.objects = ObjectTracker(limits.tracked_objects)   # this session only
        self.events = EventPublisher(self, manifest)
        self.host = HostServices(self)
        self.background = Background(self)
        self._state = WorkerState.NEW
        self._admission_closed = False
        self._initial_failure: IntegrationError | None = None
        self._grants: Grants | None = None
        self._last_host_number = 0
        self._worker_numbers = 0
        self._seq = 0
        self._handlers: dict[str, _Handler] = {}
        self._calls: dict[str, _HostCall] = {}
        self._background: dict[asyncio.Task, CallContext] = {}
        self._owned: set[asyncio.Task] = set()          # SDK tasks such as a pending receive
        self._control: deque[tuple[bytes, Callable[[], None] | None]] = deque()
        self._normal: deque[tuple[bytes, Callable[[], None] | None]] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None
        self._done: asyncio.Event | None = None
        self._reader: asyncio.Task | None = None
        self._writer: asyncio.Task | None = None
        self._stopping = False                  # shutdown or abort has begun
        self._violated = False                  # stop reading: the host broke the protocol
        self._write_failed = False              # the output stream is broken
        self._closing = False                   # writer exits once drained
        self._cleanup: asyncio.Task | None = None
        self._stop_deadline: float | None = None  # absolute: cleanup AND final output
        self._stop_timer: TimerHandle | None = None
        self._quiet_check: Callable[[], None] | None = None
        self._failure = ExitCode.CLEAN          # worst failure seen while stopping
        self._exit_code = ExitCode.UNCLEAN
        self.counters = {"provider_errors": 0, "write_failures": 0}

    # ── public state ──

    @property
    def state(self) -> WorkerState:
        return self._state

    def accounting(self) -> dict[str, int]:
        kinds = [h.kind for h in self._handlers.values()]
        return {"actions": kinds.count("action"), "control": kinds.count("control"),
                "lifecycle": kinds.count("lifecycle"), "host_calls": len(self._calls),
                "background": len(self._background), "events": len(self.outbox),
                "write_queue": len(self._control) + len(self._normal)}

    # ── run ──

    async def run(self) -> ExitCode:
        """Serve the host until shutdown or failure; returns the exit code."""
        if self._loop is not None:
            raise RuntimeError("a WorkerRuntime runs once")
        self._loop = asyncio.get_running_loop()
        if self._scheduler is None:
            self._scheduler = LoopScheduler(self._loop)
        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        try:
            self._reader = self._spawn(self._read_loop(), None)
            self._writer = self._spawn(self._write_loop(), None)
        except Exception:
            log.error("the worker could not start its reader or writer")
            if self._reader is not None:
                self._reader.cancel()
            return ExitCode.UNCLEAN
        await self._done.wait()
        # Let queued output drain, but never past the absolute stop deadline: a
        # host that stopped reading must not keep the worker alive.
        if self._stop_deadline is not None:
            remaining = self._stop_deadline - self._scheduler.now()
            if remaining > 0:
                await self._wait_for(self._writer, remaining)
        for task in (self._reader, self._writer, self._cleanup, *self._background,
                     *self._owned, *(h.task for h in self._handlers.values())):
            if task is not None and not task.done():
                task.cancel()
        return self._exit_code

    def _spawn(self, coroutine: Coroutine, call: CallContext | None) -> asyncio.Task:
        context = contextvars.copy_context()
        context.run(current_call.set, call)
        try:
            if self._spawn_task is not None:
                return self._spawn_task(coroutine, context)
            return self._loop.create_task(coroutine, context=context)
        except BaseException:
            coroutine.close()
            raise

    async def _wait_for(self, task: asyncio.Task | None, seconds: float) -> None:
        """Wait until `task` ends or `seconds` pass on the scheduler's clock."""
        if task is None or task.done():
            return
        waiter = self._loop.create_future()
        timer = self._scheduler.call_at(self._scheduler.now() + seconds,
                                        lambda: waiter.done() or waiter.set_result(None))
        task.add_done_callback(lambda _t: waiter.done() or waiter.set_result(None))
        try:
            await waiter
        finally:
            timer.cancel()

    def _finish(self, code: ExitCode) -> None:
        if self._state is WorkerState.CLOSED:
            return
        self._state = WorkerState.CLOSED
        self._exit_code = max(code, self._failure)
        if self._stop_timer is not None:
            self._stop_timer.cancel()
        self._closing = True
        self._wake.set()
        self._done.set()

    # ── writer ──

    def _enqueue(self, frame: bytes, *, control: bool,
                 after: Callable[[], None] | None = None,
                 dropped: Callable[[], None] | None = None) -> None:
        """Queue `frame`; `after` runs once it is written, `dropped` if it never will be."""
        if self._write_failed:                  # nothing can be written any more
            if dropped is not None:
                dropped()
            return
        if len(self._control) + len(self._normal) >= self._limits.write_queue:
            log.error("the write queue is full; stopping")
            self._abort(ExitCode.UNCLEAN)
            if dropped is not None:
                dropped()
            return
        (self._control if control else self._normal).append((frame, after, dropped))
        self._wake.set()

    def wake_writer(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def _next_frame(self) -> tuple[bytes, Callable | None, Callable | None] | None:
        if self._control:
            return self._control.popleft()
        if self._normal:
            return self._normal.popleft()
        # Setup state is staged until the initialize success reply is queued.
        # Control priority puts that reply before the promoted state events.
        if self._state is not WorkerState.READY:
            return None
        item = self.outbox.take()
        if item is None:
            return None
        self._seq += 1                          # assigned only now, after coalescing
        return encode_message(Event(item[0], self._seq, item[1])), None, None

    async def _write_loop(self) -> None:
        while True:
            item = self._next_frame()
            if item is None:
                if self._closing:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue
            frame, after, dropped = item
            try:
                await self._transport.write(frame)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.counters["write_failures"] += 1
                log.error("could not write to the host; stopping")
                self._write_failed = True
                lost = [dropped, *(d for _f, _a, d in (*self._control, *self._normal))]
                self._control.clear()
                self._normal.clear()
                self._abort(ExitCode.UNCLEAN)   # cleanup still runs, within its budget
                for callback in lost:
                    if callback is not None:
                        callback()
                return
            if after is not None:
                if self._stop_deadline is not None and self._scheduler.now() >= self._stop_deadline:
                    self._stop_expired()
                else:
                    after()

    # ── reader ──

    async def _read_loop(self) -> None:
        decoder = LineDecoder()
        try:
            while True:        # keeps reading during shutdown: host-call replies arrive
                chunk = await self._transport.read(self._limits.read_chunk_bytes)
                if not chunk:
                    if decoder.pending:
                        log.error("the host stream ended inside a message")
                    elif not self._stopping:
                        log.info("the host closed the protocol stream")
                    self._abort(ExitCode.UNCLEAN)
                    return
                for frame in decoder.feed(chunk):
                    self._on_frame(frame)
                    if self._violated:
                        return
        except asyncio.CancelledError:
            raise
        except DomainError:
            self._violation()
        except Exception as error:
            log.error("could not process host input (%s); stopping", type(error).__name__)
            self._abort(ExitCode.UNCLEAN)

    def _violation(self) -> None:
        log.error("the host violated the protocol; stopping")
        self._violated = True
        self._abort(ExitCode.PROTOCOL_VIOLATION)

    def _on_frame(self, frame: bytes) -> None:
        try:
            message = decode_message(frame, sender="host")
        except DomainError:
            self._violation()
            return
        if isinstance(message, Response):
            self._on_host_reply(message)
        else:
            self._on_request(message)

    # ── host requests ──

    def _reply(self, request: Request, *, result: Any = None, error: ErrorInfo | None = None,
               after: Callable[[], None] | None = None,
               dropped: Callable[[], None] | None = None) -> None:
        try:
            if error is None:
                frame = encode_message(Response(request.id, True, result=result))
            else:
                frame = encode_message(Response(request.id, False, error=error))
        except DomainError:
            frame = encode_message(Response(request.id, False, error=_error(
                ErrorCode.PROVIDER_ERROR, _INVALID_RESULT)))
        self._enqueue(frame, control=request.method != "action.invoke", after=after,
                      dropped=dropped)

    def _on_request(self, request: Request) -> None:
        number = request_number(request.id)
        if number <= self._last_host_number:
            self._violation()
            return
        self._last_host_number = number
        method = request.method
        if method == "session.shutdown":
            self._begin_shutdown(request)
            return
        if self._stopping or self._admission_closed:
            self._reply(request, error=_error(ErrorCode.INSTANCE_UNAVAILABLE,
                                              "The integration is stopping."))
            return
        if method == "session.initialize":
            if self._state is not WorkerState.NEW:
                self._reply(request, error=_error(ErrorCode.INVALID_ARGUMENT,
                                                  "The integration is already initialized."))
                return
            self._start_initialize(request)
            return
        if self._state is not WorkerState.READY:
            self._reply(request, error=_error(ErrorCode.INSTANCE_UNAVAILABLE,
                                              "The integration is not ready."))
            return
        if method == "action.invoke":
            self._start_action(request)
        elif method in ("ui.menu.event", "ui.menu.children"):
            if self._control_full(request):
                return
            params = thaw_json(request.params)
            if method == "ui.menu.event":
                async def event():
                    await self.provider.on_menu_event(params["menu_id"], params["event"])
                    return {}
                coroutine = event()
            else:
                coroutine = _call(self.provider.menu_children, params["menu_id"], params["page_id"], params["cursor"])
            from ._wire.validation import parse_method_result
            self._start_handler(request, "control", coroutine, lambda result: parse_method_result(method, result))
        elif method == "discovery.list":
            self._start_discovery(request)
        else:
            self._start_snapshot(request)

    def _start_initialize(self, request: Request) -> None:
        params = thaw_json(request.params)
        try:
            grants = self._check_initialize(params)
        except DomainError as error:
            self._reply(request, error=ErrorInfo(error.code, error.info().message))
            return
        except IntegrationError as error:
            self._reply(request, error=error.info)
            return
        self._ui_reduced_motion = params.get("ui_capabilities", {}).get("reduced_motion", False)
        self._grants = Grants(grants)
        setup = Setup(self.manifest.id, self.manifest.version, params["config"],
                      params["config_schema_version"], self._grants, self.host, self.events,
                      self.background)
        self._state = WorkerState.INITIALIZING
        self._start_handler(request, "lifecycle", _call(self.provider.initialize, setup),
                            self._initialized)

    def _check_initialize(self, params: dict):
        def wrong(field: str, message: str) -> DomainError:
            return DomainError(ErrorCode.INVALID_ARGUMENT, ("params", field), message)
        if params["package_id"] != self.manifest.id:
            raise wrong("package_id", "does not match this package")
        if params["package_version"] != self.manifest.version:
            raise wrong("package_version", "does not match this package version")
        if params["host_protocol_version"] != policy.PROTOCOL_VERSION:
            raise wrong("host_protocol_version", "is not supported by this worker")
        if params["config_schema_version"] != self.manifest.config_schema_version:
            raise wrong("config_schema_version", "does not match this package")
        try:
            params["config"] = validate_config(self.manifest, params["config"])
        except DomainError as error:
            raise error.at(("params", "config")) from None
        grants = parse_grant_set(params["grants"], ("params", "grants"))
        try:
            validate_grants(self.manifest, grants)
        except DomainError as error:
            raise error.at(("params", "grants")) from None
        view = Grants(grants)
        if any(p.required_for_initialization and not view.is_granted(p.id)
               for p in self.manifest.permissions):
            raise IntegrationError(ErrorCode.NEEDS_SETUP,
                                   "A permission this integration needs is not granted.")
        return grants

    def _initialized(self, _value: Any) -> dict:
        return {"package_id": self.manifest.id, "package_version": self.manifest.version,
                "protocol_version": policy.PROTOCOL_VERSION}

    def _start_action(self, request: Request) -> None:
        params = request.params
        try:
            action = resolve_action(self.manifest, params["action_id"])
            try:
                arguments = validate_arguments(action, thaw_json(params["arguments"]))
            except DomainError as error:
                raise error.at(("arguments",)) from None
        except DomainError as error:
            error = error.at(("params",))
            self._reply(request, error=ErrorInfo(error.code, error.info().message))
            return
        if not all(self._grants.is_granted(p) for p in action.permissions):
            self._reply(request, error=_error(ErrorCode.PERMISSION_DENIED,
                                              "A permission this action needs is not granted."))
            return
        if self.accounting()["actions"] >= self._limits.action_handlers:
            self._reply(request, error=_error(ErrorCode.SATURATED,
                                              "Too many actions are already running."))
            return
        self._start_handler(request, "action", _call(self._actions[action.id], arguments),
                            lambda value: validate_result(action, value))

    def _start_discovery(self, request: Request) -> None:
        collection_id, cursor = request.params["collection_id"], request.params["cursor"]
        try:
            self.manifest.collection(collection_id)
        except DomainError:
            self._reply(request, error=_error(ErrorCode.INVALID_ARGUMENT,
                                              "/params/collection_id: is not a declared "
                                              "collection"))
            return
        if self._control_full(request):
            return
        self._start_handler(request, "control",
                            _call(self.provider.list_objects, collection_id, cursor),
                            lambda value: self._page_result(collection_id, value))

    def _start_snapshot(self, request: Request) -> None:
        object_ids = tuple(request.params["object_ids"])
        if self._control_full(request):
            return
        self._start_handler(request, "control", _call(self.provider.read_states, object_ids),
                            lambda value: self._states_result(object_ids, value))

    def _control_full(self, request: Request) -> bool:
        if self.accounting()["control"] < self._limits.control_handlers:
            return False
        self._reply(request, error=_error(ErrorCode.SATURATED,
                                          "Too many requests are already running."))
        return True

    def _page_result(self, collection_id: str, value: Any) -> dict:
        if not isinstance(value, Page):
            raise DomainError(ErrorCode.PROTOCOL_ERROR, (), "is not a Page")
        page = DiscoveryPage([ObjectRecord(o.id, o.collection_id, o.label, o.parent_id,
                                           tuple(o.capabilities)) for o in value.objects],
                             value.next_cursor)
        return validate_discovery_page(self.manifest, collection_id, page).to_json()

    def _states_result(self, object_ids: tuple[str, ...], value: Any) -> dict:
        if not isinstance(value, Sequence) or len(value) > policy.DISCOVERY_PAGE_MAX:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, (), "is not a list of states")
        records, identity = [], []
        for state in value:
            if not isinstance(state, State) or state.object_id not in object_ids:
                raise DomainError(ErrorCode.PROTOCOL_ERROR, (), "is not a requested state")
            record = StateRecord(state.object_id, state.revision, state.values, state.available)
            collection = self.manifest.collection(state.collection_id)
            validate_state_values(collection, record)
            records.append(record)
            identity.append((record.object_id, collection.id, record.revision,
                             state_digest(record)))
        check_unique(tuple(r.object_id for r in records), ("states",), "object ID")
        try:
            self.objects.check(identity)          # the whole batch, before any change
        except ValueError:
            raise DomainError(ErrorCode.PROTOCOL_ERROR, (), "is inconsistent with "
                              "earlier state") from None
        self.objects.commit(identity)
        return {"states": [r.to_json() for r in records]}

    # ── handler tasks ──

    def _start_handler(self, request: Request, kind: str, coroutine: Coroutine,
                       finish: Callable[[Any], Any]) -> None:
        context = CallContext(request.id, self._scheduler.now() + request.timeout_ms / 1000)
        handler = _Handler(request, kind, context, finish)
        try:
            handler.task = self._spawn(coroutine, context)
        except Exception:
            if kind == "lifecycle":
                self._state = WorkerState.NEW
            self._reply(request, error=_error(ErrorCode.PROVIDER_ERROR,
                                              "The integration could not start the request."))
            return
        self._handlers[request.id] = handler
        handler.timer = self._scheduler.call_at(context.deadline, lambda: self._expire(handler))
        handler.task.add_done_callback(lambda _task: self._on_handler_done(handler))

    def _expire(self, handler: _Handler) -> None:
        if not handler.task.done():
            handler.cancel_reason = handler.cancel_reason or "deadline"
            handler.task.cancel()

    def _outcome(self, handler: _Handler) -> tuple[bool, Any]:
        """(True, value) or (False, ErrorInfo) for a finished handler task."""
        task = handler.task
        if task.cancelled():
            if handler.cancel_reason == "deadline":
                return False, _error(ErrorCode.TIMEOUT, "The operation did not finish in time.",
                                     outcome_unknown=True)
            return False, _error(ErrorCode.CANCELLED, "The integration stopped before the "
                                 "operation finished.", outcome_unknown=True)
        exception = task.exception()
        if exception is not None:
            if not isinstance(exception, IntegrationError):
                self.counters["provider_errors"] += 1
                log.warning("%s failed with an unexpected error", handler.request.method)
            return False, safe_error(exception)
        if handler.cancel_reason == "shutdown":
            # Cleanup already ran; a value produced after that is not trusted.
            return False, _error(ErrorCode.CANCELLED, "The integration stopped before the "
                                 "operation finished.", outcome_unknown=True)
        return True, task.result()

    def _on_handler_done(self, handler: _Handler) -> None:
        handler.context.finished = True
        handler.timer.cancel()
        self._handlers.pop(handler.request.id, None)
        if self._state is WorkerState.CLOSED:
            if not handler.task.cancelled():
                handler.task.exception()
            return
        ok, value = self._outcome(handler)
        if ok and handler.kind == "lifecycle" and self._initial_failure is not None:
            ok, value = False, safe_error(self._initial_failure)
        if ok:
            try:
                value = handler.finish(value)
            except DomainError:
                log.warning("%s returned an invalid result", handler.request.method)
                ok, value = False, _error(ErrorCode.PROVIDER_ERROR, _INVALID_RESULT)
            except IntegrationError as error:      # e.g. TooManyObjects
                ok, value = False, safe_error(error)
        if handler.kind == "lifecycle":
            if ok and self._state is WorkerState.INITIALIZING:
                self._state = WorkerState.READY      # before the reply is queued
            elif self._state is WorkerState.INITIALIZING:
                self.outbox.clear()                  # failed setup never publishes state
                self._state = WorkerState.STOPPING
                self._admission_closed = True
        if ok:
            self._reply(handler.request, result=value)
        else:
            self._reply(handler.request, error=value)
        self._check_quiet()

    # ── host calls ──

    def remaining_s(self) -> float | None:
        call = current_call.get()
        if call is None or call.deadline is None:
            return None
        return max(0.0, call.deadline - self._scheduler.now())

    async def host_call(self, method: str, params: dict, timeout_s: float, *,
                        detached: bool = False) -> Any:
        """One worker-to-host call, correlated with the request being handled.

        `detached` makes it a background call whatever the current context:
        no parent, and no inherited deadline. Background calls need Ready,
        except that a connection's own operations may also run while the
        worker initializes (CONNECTIONS.md), so a connection reader started
        during initialize outlives that request.
        """
        if detached:
            call = CallContext(None, None)
        else:
            call = current_call.get()
            if call is None:
                raise ContextError("host calls need the request being handled; use "
                                   "Setup.background.start() for independent work")
            if call.finished:
                raise ContextError("the request this work belonged to has finished; use "
                                   "Setup.background.start() for work that outlives it")
        connecting = self._state is WorkerState.INITIALIZING and method in WEBSOCKET_METHODS
        if self._stopping or (call.request_id is None and self._state is not WorkerState.READY
                              and not connecting):
            raise NotReadyError()
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) \
                or not timeout_s > 0:
            raise ValueError("timeout_s must be a positive number of seconds")
        now = self._scheduler.now()
        budget = min(float(timeout_s), policy.HOST_SERVICE_TIMEOUT_MAX_S)
        if call.deadline is not None:
            budget = min(budget, call.deadline - now)
        timeout_ms = int(budget * 1000)              # rounded down: never past the parent
        if timeout_ms < 1:
            raise HostCallTimeout()
        if len(self._calls) >= self._limits.host_calls:
            raise IntegrationError(ErrorCode.SATURATED, "Too many host calls are in progress.")
        request_id = f"w-{self._worker_numbers + 1}"
        try:
            frame = encode_message(Request(request_id, method, timeout_ms,
                                           dict(params, parent_request_id=call.request_id)))
        except DomainError as error:
            raise ValueError(f"invalid host call {error.pointer}: {error.message}") from None
        self._worker_numbers += 1
        pending = _HostCall(request_id, method, now + timeout_ms / 1000)
        pending.waiter = waiter = self._loop.create_future()
        pending.timer = self._scheduler.call_at(
            pending.deadline, lambda: waiter.done() or waiter.set_result(_TIMED_OUT))
        self._calls[request_id] = pending
        self._enqueue(frame, control=True)
        outcome = await waiter                         # the call stays pending if cancelled
        if outcome is _TIMED_OUT:
            raise HostCallTimeout()
        if isinstance(outcome, HostCallError):
            raise outcome
        return outcome

    def _on_host_reply(self, response: Response) -> None:
        pending = self._calls.pop(response.id, None)
        if pending is None:
            self._violation()
            return
        pending.timer.cancel()
        if response.ok:
            try:
                outcome = parse_method_result(pending.method, thaw_json(response.result))
            except DomainError:
                self._violation()
                return
        else:
            outcome = HostCallError.from_info(response.error)
        waiter = pending.waiter
        if not waiter.done():
            late = self._scheduler.now() >= pending.deadline
            waiter.set_result(_TIMED_OUT if late else outcome)

    # ── background work and events ──

    def start_background(self, work: Callable[[], Awaitable[Any]], name: str) -> None:
        if self._stopping or self._admission_closed or self._loop is None:
            raise NotReadyError()
        if len(self._background) >= self._limits.background_tasks:
            raise IntegrationError(ErrorCode.SATURATED, "Too much background work is running.")
        context = CallContext(None, None)
        try:
            task = self._spawn(_call(work), context)
        except Exception:
            raise IntegrationError(ErrorCode.PROVIDER_ERROR,
                                   "Background work could not start.") from None
        self._background[task] = context
        task.add_done_callback(self._on_background_done)

    def _on_background_done(self, task: asyncio.Task) -> None:
        context = self._background.pop(task, None)
        if context is not None:
            context.finished = True
        if not task.cancelled() and task.exception() is not None:
            log.warning("background work failed")
        self._check_quiet()

    @staticmethod
    def check_event_frame(name: str, data: dict) -> None:
        """Refuse an event the wire could not carry (for example too large)."""
        encode_message(Event(name, 1, data))

    def require_publishing(self, *, initialization: bool = False) -> None:
        allowed = (WorkerState.INITIALIZING, WorkerState.READY) if initialization else (WorkerState.READY,)
        if self._state not in allowed or self._admission_closed or self._stopping:
            raise NotReadyError()

    def fail_initialization(self, error: IntegrationError) -> bool:
        if self._state is not WorkerState.INITIALIZING:
            return False
        self._initial_failure = error
        self.close_admission()
        return True

    def close_admission(self) -> None:
        self._admission_closed = True

    # ── shutdown ──

    def _begin_shutdown(self, request: Request) -> None:
        if self._stopping:
            self._reply(request, error=_error(ErrorCode.INVALID_ARGUMENT,
                                              "The integration is already stopping."))
            return
        now = self._scheduler.now()
        stop_by = now + request.timeout_ms / 1000
        self._begin_stopping(stop_by)
        reply_by = max(now, stop_by - self._limits.shutdown_reply_margin_s)
        self._start_cleanup(self._shut_down(reply_by, request), CallContext(request.id, stop_by))

    def _abort(self, code: ExitCode) -> None:
        """Stop without a host request (EOF, violation, broken pipe); bounded cleanup."""
        self._failure = max(self._failure, code)
        if self._stopping:
            return
        cleanup_by = self._scheduler.now() + self._limits.abort_cleanup_s
        self._begin_stopping(cleanup_by + self._limits.shutdown_reply_margin_s)
        self._start_cleanup(self._shut_down(cleanup_by, None), None)

    def _begin_stopping(self, stop_by: float) -> None:
        """Close admission, cancel owned work, and arm the absolute stop deadline.

        By `stop_by` the worker finishes whatever happened: provider cleanup
        still running, a final reply not written (the host stopped reading)
        or never queued. The result is then unclean; a clean exit is reported
        only after the final reply was actually written.
        """
        self._stopping = True
        self._admission_closed = True
        self._state = WorkerState.STOPPING
        self._stop_deadline = stop_by
        self._stop_timer = self._scheduler.call_at(stop_by, self._stop_expired)
        self._cancel_owned()

    def _stop_expired(self) -> None:
        if self._state is not WorkerState.CLOSED:
            log.error("the worker did not finish stopping in time")
            self._finish(ExitCode.UNCLEAN)

    def _start_cleanup(self, coroutine: Coroutine, context: CallContext | None) -> None:
        try:
            self._cleanup = self._spawn(coroutine, context)
        except Exception:
            log.error("cleanup could not start")
            self._finish(ExitCode.UNCLEAN)

    def _cancel_owned(self) -> None:
        for handler in list(self._handlers.values()):
            handler.cancel_reason = "shutdown"
            handler.task.cancel()
        for task in list(self._background):
            task.cancel()
        for task in list(self._owned):
            task.cancel()

    def own(self, coroutine: Coroutine) -> asyncio.Task:
        """Run an SDK coroutine as a task the worker owns and cancels at stop.

        It runs with a background identity. Its exception is retrieved (and
        never logged with its text); waiters read its result themselves.
        """
        task = self._spawn(coroutine, CallContext(None, None))
        self._owned.add(task)

        def done(task: asyncio.Task) -> None:
            self._owned.discard(task)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(done)
        return task

    async def _shut_down(self, deadline: float, request: Request | None) -> None:
        provider_task = None
        try:
            provider_task = self._spawn(_call(self.provider.shutdown), current_call.get())
        except Exception:
            log.error("the integration's shutdown could not start")
        quiet = await self._quiet(deadline, provider_task)
        clean = quiet and provider_task is not None and not provider_task.cancelled() \
            and provider_task.exception() is None
        if provider_task is not None and not provider_task.done():
            provider_task.cancel()
        if not clean:
            log.warning("cleanup did not finish within the shutdown budget")
        if request is None:
            self._finish(ExitCode.UNCLEAN)       # raised to the recorded failure, if worse
            return
        if self._scheduler.now() >= self._stop_deadline:
            self._finish(ExitCode.UNCLEAN)       # too late for any reply to count
            return
        final = ExitCode.CLEAN if clean else ExitCode.UNCLEAN
        unclean = lambda: self._finish(ExitCode.UNCLEAN)   # noqa: E731
        if clean:
            self._reply(request, result={}, after=lambda: self._finish(final), dropped=unclean)
        else:
            self._reply(request, error=_error(
                ErrorCode.PROVIDER_ERROR, "The integration could not finish cleaning up in time.",
                outcome_unknown=True), after=lambda: self._finish(final), dropped=unclean)

    async def _quiet(self, deadline: float, provider_task: asyncio.Task | None) -> bool:
        """Wait until every owned task ended, or the deadline; True if quiet."""
        waiter = self._loop.create_future()

        def check() -> None:
            if waiter.done():
                return
            if (not self._handlers and not self._background
                    and (provider_task is None or provider_task.done())):
                waiter.set_result(True)
            elif self._scheduler.now() >= deadline:
                waiter.set_result(False)

        timer = self._scheduler.call_at(deadline, check)
        if provider_task is not None:
            provider_task.add_done_callback(lambda _t: check())
        self._quiet_check = check
        check()
        try:
            return await waiter
        finally:
            timer.cancel()
            self._quiet_check = None

    def _check_quiet(self) -> None:
        if self._quiet_check is not None:
            self._quiet_check()
