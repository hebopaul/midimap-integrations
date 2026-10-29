"""Publishing state and occurrence events to MIDImap.

Ownership: the integration owns each object's state `revision`. Within a
worker session it may stay equal, never decrease, and an object stays in one
collection; an equal revision must carry identical values and availability
(change the revision whenever the state changes). One bounded `ObjectTracker`
per session enforces this for published state and snapshot results alike,
so the two can never disagree; it tracks at most `CACHED_OBJECTS_MAX`
objects and never forgets one, so an old revision can never become new
again. MIDImap renews an unchanged state's freshness (TTL) only from an
explicit `read_states` answer at an equal revision with identical content;
republishing the same state does not renew it, so never invent revisions to
keep state fresh. The SDK
owns the wire sequence number `seq`,
assigned 1, 2, 3, ... only when an event is actually written, after any
coalescing. Only state coalesces: a newer state for an object that is still
waiting replaces the waiting one in place. Occurrences are never merged or
dropped; when the bounded queue is full, publishing raises `EventQueueFull`
and nothing is queued. `objects.changed` invalidations coalesce per
collection: one still waiting covers every later change, because MIDImap
rescans only after receiving it. Initial state is staged in this same bounded outbox,
promoted after the initialize success reply and discarded on failure. An
unavailability report during initialize fails initialization. Occurrence events
require Ready. Publication is refused after shutdown or an unavailability
report closes admission.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Sequence
import hashlib
import json
from typing import TYPE_CHECKING, Any

from ._wire.contracts import DomainError, ErrorCode, ObjectEvent, StateRecord, thaw_json
from ._wire.validation import validate_object_event, validate_state_values
from .errors import EventQueueFull, IntegrationError, TooManyObjects
from .provider import State

if TYPE_CHECKING:
    from ._wire.contracts import PackageManifest
    from .runtime import WorkerRuntime


def state_digest(record: StateRecord) -> bytes:
    """Digest of a state's content; JSON types matter (true, 1 and 1.0 differ)."""
    text = json.dumps([thaw_json(record.values), record.available], sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


class ObjectTracker:
    """Latest collection, revision and content of every object reported in one session.

    `check` validates a whole batch without changing anything; `commit`
    records it. Objects are never evicted: once `max_objects` distinct objects
    are known, a new one is refused (`TooManyObjects`) while known ones stay
    updatable.
    """

    def __init__(self, max_objects: int):
        self.max_objects = max_objects
        self._known: dict[str, tuple[str, int, bytes]] = {}

    def __len__(self) -> int:
        return len(self._known)

    def check(self, states: Sequence[tuple[str, str, int, bytes]]) -> None:
        """Raise ValueError or TooManyObjects for (object, collection, revision, digest)s."""
        new, batch = set(), {}
        for object_id, collection_id, revision, digest in states:
            known = batch.get(object_id) or self._known.get(object_id)
            if known is None:
                new.add(object_id)
            elif known[0] != collection_id:
                raise ValueError("an object's collection must not change within a session")
            elif revision < known[1]:
                raise ValueError("state revision must not decrease within a session")
            elif revision == known[1] and digest != known[2]:
                raise ValueError("state changed without a new revision")
            batch[object_id] = (collection_id, revision, digest)
        if len(self._known) + len(new) > self.max_objects:
            raise TooManyObjects()

    def commit(self, states: Sequence[tuple[str, str, int, bytes]]) -> None:
        for object_id, collection_id, revision, digest in states:
            self._known[object_id] = (collection_id, revision, digest)


class EventOutbox:
    """Events waiting for the writer. One extra slot is kept for unavailability."""

    def __init__(self, max_items: int):
        self.max_items = max_items
        self._items: deque[list] = deque()          # [name, data]
        self._states: dict[str, list] = {}
        self._changed: dict[str, list] = {}         # waiting invalidations by collection
        self._unavailable: tuple[str, dict] | None = None

    def __len__(self) -> int:
        return len(self._items) + (self._unavailable is not None)

    def clear(self) -> None:
        self._items.clear()
        self._states.clear()
        self._changed.clear()
        self._unavailable = None

    def put_state(self, record: StateRecord) -> None:
        slot = self._states.get(record.object_id)
        if slot is not None:
            slot[1] = record.to_json()
            return
        self._append(["state.changed", record.to_json()])
        self._states[record.object_id] = self._items[-1]

    def put_changed(self, collection_id: str) -> None:
        if collection_id in self._changed:
            return
        self._append(["objects.changed", {"collection_id": collection_id}])
        self._changed[collection_id] = self._items[-1]

    def put(self, name: str, data: dict) -> None:
        self._append([name, data])

    def put_unavailable(self, data: dict) -> None:
        if self._unavailable is None:
            self._unavailable = ("instance.unavailable", data)

    def _append(self, slot: list) -> None:
        if len(self._items) >= self.max_items:
            raise EventQueueFull()
        self._items.append(slot)

    def take(self) -> tuple[str, dict] | None:
        if self._items:
            name, data = self._items.popleft()
            if name == "state.changed":
                del self._states[data["object_id"]]
            elif name == "objects.changed":
                del self._changed[data["collection_id"]]
            return name, data
        if self._unavailable is not None:
            item, self._unavailable = self._unavailable, None
            return item
        return None


class EventPublisher:
    """Event publishing available as `Setup.events`.

    Mistakes in published values (undeclared IDs, schema violations, a
    decreasing revision, a changed collection) raise `ValueError` naming the
    field, not the value. A new object beyond the session's object limit
    raises `TooManyObjects`. Nothing is queued or recorded when publishing fails.
    """

    def __init__(self, runtime: WorkerRuntime, manifest: PackageManifest):
        self._runtime = runtime
        self._manifest = manifest

    def publish_state(self, state: State) -> None:
        self._runtime.require_publishing(initialization=True)
        try:
            collection = self._manifest.collection(state.collection_id)
            record = StateRecord(state.object_id, state.revision, state.values, state.available)
            validate_state_values(collection, record)
            self._runtime.check_event_frame("state.changed", record.to_json())
        except DomainError as error:
            raise _invalid(error) from None
        identity = [(record.object_id, collection.id, record.revision, state_digest(record))]
        self._runtime.objects.check(identity)
        self._runtime.outbox.put_state(record)
        self._runtime.objects.commit(identity)
        self._runtime.wake_writer()

    def publish_event(self, object_id: str, event_id: str, payload: Any) -> None:
        self._runtime.require_publishing()
        try:
            event = ObjectEvent(object_id, event_id, payload)
            validate_object_event(self._manifest, event)
            data = {"object_id": event.object_id, "event_id": event.event_id,
                    "payload": thaw_json(event.payload)}
            self._runtime.check_event_frame("object.event", data)
        except DomainError as error:
            raise _invalid(error) from None
        self._runtime.outbox.put("object.event", data)
        self._runtime.wake_writer()

    def publish_objects_changed(self, collection_id: str) -> None:
        """Tell MIDImap that objects of a collection were added, removed or renamed.

        An invalidation, not a list: MIDImap rescans the collection through
        `list_objects` when something shows it. Call it for every such change
        (and when a provider-side catalog is replaced); notices still waiting
        for one collection are merged. Allowed while initializing and Ready.
        Raises `ValueError` for an undeclared collection and `EventQueueFull`
        when no notice for it is waiting and the queue is full.
        """
        self._runtime.require_publishing(initialization=True)
        try:
            collection = self._manifest.collection(collection_id)
        except DomainError as error:
            raise _invalid(error) from None
        self._runtime.outbox.put_changed(collection.id)
        self._runtime.wake_writer()

    def report_unavailable(self, code: ErrorCode | str, message: str, *, retryable: bool = False) -> None:
        """Tell MIDImap the instance cannot work now; this closes admission.

        MIDImap then stops this worker; opted-in packages may reconnect after
        explicitly retryable connection failures. Absent/false preserves manual recovery. The
        message follows the same rules as `IntegrationError` messages.
        """
        self._runtime.require_publishing(initialization=True)
        error = IntegrationError(code, message, retryable=retryable)
        if self._runtime.fail_initialization(error):
            return
        data = {"code": error.code.value, "message": error.message}
        if retryable:
            data["retryable"] = True
        self._runtime.outbox.put_unavailable(data)
        self._runtime.close_admission()
        self._runtime.wake_writer()


def _invalid(error: DomainError) -> ValueError:
    return ValueError(f"invalid event {error.pointer or '<root>'}: {error.message}")
