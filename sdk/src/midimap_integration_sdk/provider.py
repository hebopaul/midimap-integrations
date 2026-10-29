"""What an integration implements: a `Provider` with action methods.

Subclass `Provider`, decorate one coroutine method per manifest action with
`@action("<action id>")`, and override the discovery/state/lifecycle methods
your package needs. The SDK validates every argument before calling you and
every value you return before sending it, against your manifest's schemas.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ._wire.contracts import ErrorCode, GrantDecision, GrantSet
from .errors import IntegrationError

if TYPE_CHECKING:
    from .events import EventPublisher
    from .host import Background, HostServices

_ACTION_ATTRIBUTE = "__midimap_action__"


def action(action_id: str) -> Callable[[Callable], Callable]:
    """Mark a `Provider` coroutine method as the handler of a manifest action.

    The method receives the validated arguments (a new `dict`) and returns
    the action result, which must match the action's `output_schema`.
    """
    def mark(function: Callable) -> Callable:
        setattr(function, _ACTION_ATTRIBUTE, action_id)
        return function
    return mark


def action_handlers(provider: Provider) -> dict[str, Callable[[dict], Awaitable[Any]]]:
    """The provider's bound action methods keyed by action ID."""
    handlers: dict[str, Callable] = {}
    for name in dir(type(provider)):
        action_id = getattr(getattr(type(provider), name, None), _ACTION_ATTRIBUTE, None)
        if action_id is None:
            continue
        if action_id in handlers:
            raise ValueError(f"action {action_id!r} has more than one handler")
        handlers[action_id] = getattr(provider, name)
    return handlers


@dataclass(frozen=True)
class ObjectInfo:
    """A discovered object. IDs are stable strings, not labels or indexes."""
    id: str
    collection_id: str
    label: str
    parent_id: str | None = None
    capabilities: Sequence[str] = ()          # IDs of declared actions


@dataclass(frozen=True)
class Page:
    """One page of discovery results; `next_cursor` is None on the last page."""
    objects: Sequence[ObjectInfo]
    next_cursor: str | None = None


@dataclass(frozen=True)
class State:
    """Current state of one object.

    The integration owns `revision`: a non-negative number that increases
    whenever the object's state changes within a worker session. The SDK owns
    event sequence numbers. `values` must match the collection's
    `state_schema`.
    """
    object_id: str
    collection_id: str
    revision: int
    values: Mapping[str, Any]
    available: bool = True


class Grants:
    """Read-only view of the user's decisions for this instance.

    Use it for clear early errors. The MIDImap host checks grants again for
    every host-service call; this view grants nothing by itself.
    """

    def __init__(self, grants: GrantSet):
        self._granted = frozenset(g.permission_id for g in grants.decisions
                                  if g.decision is GrantDecision.GRANTED)
        self.revision = grants.revision

    def is_granted(self, permission_id: str) -> bool:
        return permission_id in self._granted


@dataclass(frozen=True)
class Setup:
    """Everything an initialized integration receives from the SDK."""
    package_id: str
    package_version: str
    config: Mapping[str, Any]                 # validated against config_schema
    config_schema_version: int
    grants: Grants
    host: HostServices
    events: EventPublisher
    background: Background


    @property
    def ui(self):
        from .ui import UI
        return UI(self.host._runtime)


class Provider:
    """Base class for an integration's implementation.

    Construct it yourself with whatever it needs (constructor injection) and
    pass it to `run_stdio`. Nothing here performs I/O until the SDK calls it.
    """

    async def initialize(self, setup: Setup) -> None:
        """Finish every required setup step, then return.

        MIDImap reports the instance Ready only after this returns, so do not
        return while required access is still being established in the
        background. Raise `IntegrationError` (for example `NEEDS_SETUP`) if
        the instance cannot work yet.
        """

    async def list_objects(self, collection_id: str, cursor: str | None) -> Page:
        raise IntegrationError(ErrorCode.INVALID_ARGUMENT,
                               "This integration has no objects to discover.")

    async def read_states(self, object_ids: Sequence[str]) -> Sequence[State]:
        raise IntegrationError(ErrorCode.INVALID_ARGUMENT,
                               "This integration does not report object state.")

    async def on_menu_event(self, menu_id: str, event: Mapping[str, Any]) -> None:
        """Handle select/alternate/value_changed/closed from the current menu."""

    async def menu_children(self, menu_id: str, page_id: str, cursor: str | None):
        """Return {spec: a menu spec, next_cursor: str | None} for lazy pages."""
        raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "This menu has no dynamic pages.")

    async def shutdown(self) -> None:
        """Close connections and stop your own work; the SDK cancels its tasks."""
