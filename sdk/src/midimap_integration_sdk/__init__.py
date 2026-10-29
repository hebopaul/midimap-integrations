"""MIDImap integration SDK for Python (draft protocol v1).

Write a `Provider`, mark its action methods with `@action`, and start it from
your worker's entry point with `run_worker(factory, package_manifest(__file__))`,
where `factory` imports and creates the provider after the protocol streams
are claimed. The SDK handles framing, validation, deadlines, host calls and
events. Importing this package starts nothing and leaves stdio untouched.

Packages are trusted executable code. The SDK is not a sandbox, and the
permissions users grant control MIDImap-owned services only.
"""
from ._wire.contracts import ErrorCode
from .errors import (
    ContextError, EventQueueFull, HostCallError, HostCallTimeout, IntegrationError,
    NotReadyError, TooManyObjects, WebSocketClosed,
)
from .events import EventPublisher
from .host import (
    AccessToken, Background, HostServices, WebSocket, WebSessionInfo, WebSessionResponse,
)
from .ui import UI
from . import ui
from .provider import Grants, ObjectInfo, Page, Provider, Setup, State, action
from .runtime import ExitCode, WorkerLimits, WorkerRuntime, WorkerState
from .stdio import load_manifest, package_manifest, run_worker

__version__ = "0.1.0"

__all__ = [
    "UI", "ui", "AccessToken", "Background", "ContextError", "ErrorCode", "EventPublisher", "EventQueueFull",
    "ExitCode", "Grants", "HostCallError", "HostCallTimeout", "HostServices",
    "IntegrationError", "NotReadyError", "ObjectInfo", "Page", "Provider", "Setup",
    "State", "TooManyObjects", "WebSocket", "WebSocketClosed", "WebSessionInfo",
    "WebSessionResponse", "WorkerLimits",
    "WorkerRuntime", "WorkerState", "action", "load_manifest", "package_manifest", "run_worker",
]
