"""MIDImap reference integration for OBS Studio (development slice, C5).

Objects:

* `scenes`: `scene:<sceneUuid>`, state `{"program": bool}`, action
  `switch_scene`. Listed in OBS's scene order as of the last full load;
  scenes created since are listed last.
* `inputs`: `input:<inputUuid>`, state `{"muted": bool}`, action
  `set_input_mute`. Only inputs with audio are listed. Required UUID and
  audio capability features must be present. Target: OBS 32.0.2 / 5.6.3.

Identity is OBS's UUID (obs-websocket 5.4.0 and later), so renaming a scene
or input keeps its ID and every mapping that uses it. Only the UUID is ever
sent: obs-websocket resolves a request naming both a name and a UUID by the
name. A target that no longer exists fails with TARGET_MISSING; another
object is never chosen in its place.

Every addition, removal and rename of a listed scene or input, and every
scene collection change, publishes `objects.changed` for its collection, so
MIDImap rescans open pickers. Listing pages carry the catalog version: if
the scenes or inputs change between two pages, the next page fails
(retryable) instead of silently skipping or repeating an object, and the
invalidation makes MIDImap scan again.

`initialize` connects, signs in if OBS asks, subscribes, and loads every
scene, input and mute state before returning, so Ready means a complete
snapshot. One reader receives everything; actions wait only for their own
answer, so events keep being applied while actions are pending. Events that
arrive while a load is outstanding are applied on top of it in order. When
the connection drops, the instance reports itself unavailable (MIDImap marks
its cached state stale) and nothing is sent again: a request that may have
reached OBS fails with `outcome_unknown`, never a replay.

The shipped transport uses Setup.host.open_websocket. A connector may be
injected by deterministic tests only.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

from midimap_integration_sdk import (
    ErrorCode, EventQueueFull, HostCallError, IntegrationError, NotReadyError, ObjectInfo,
    Page, Provider, Setup, State, WebSocketClosed, action,
)

from obs_protocol import (
    CAPS_VERSION, CLOSE_AUTHENTICATION_FAILED, CLOSE_SESSION_INVALIDATED,
    CLOSE_UNSUPPORTED_RPC_VERSION, STATUS_INVALID_RESOURCE_STATE, STATUS_INVALID_RESOURCE_TYPE,
    STATUS_NOT_READY, STATUS_RESOURCE_NOT_FOUND, UUID_VERSION, Busy, Closed, Connector,
    ObsConnection, ProtocolFailure, RequestFailed, RequiredFeaturesMissing,
)

SCENES, INPUTS = "scenes", "inputs"
SCENE_PREFIX, INPUT_PREFIX = "scene:", "input:"
PASSWORD_FIELD = "server_password"
OBS_SOURCE_AUDIO = 1 << 1             # libobs obs-source.h
PAGE_SIZE = 100                       # MIDImap's discovery page limit
KNOWN_MAX = 4000                      # scenes and inputs seen in one session
LABEL_MAX = 128
MUTE_READS_AT_ONCE = 6                 # leave room for receive in the 8 host-call slots
BUFFERED_EVENTS_MAX = 256
BUFFERED_BYTES_MAX = 1024 * 1024

# Fixed messages. OBS text (request comments, close reasons) is never shown.
UNREACHABLE = "OBS did not answer. Check that OBS is running and its WebSocket server is on."
PASSWORD_NEEDED = "OBS asks for a password. Enter the server password in setup and allow it to be read."
PASSWORD_UNREADABLE = "The saved OBS password could not be read; try again."
PASSWORD_REFUSED = "OBS did not accept the password. Check the server password in setup."
TOO_OLD = "OBS lacks required UUID or audio capability features. Target: OBS 32.0.2 / obs-websocket 5.6.3."
UNSUPPORTED = "This OBS version does not support this integration's protocol."
UNEXPECTED = "OBS sent something this integration does not understand."
NOT_READY_YET = "OBS is not ready yet; try again in a moment."
LOST = "The connection to OBS was lost."
EXITED = "OBS closed."
ENDED = "OBS ended the connection."
NOT_CONNECTED = "OBS is not connected."
LOST_DURING_ACTION = "The connection to OBS was lost; OBS may have carried out the action."
SWITCHING = "OBS is switching scene collections; try again in a moment."
LIST_CHANGED = "The OBS scenes or inputs changed while MIDImap read them; it reads them again."
BUSY = "Too many OBS requests are waiting; try again."
TOO_MANY = "OBS has more scenes and inputs than this integration supports."
TOO_MANY_CHANGES = "OBS sent more changes than MIDImap could take in at once."
MISSING_SCENE = "The selected scene no longer exists in OBS."
MISSING_INPUT = "The selected input no longer exists in OBS."
NO_AUDIO = "The selected input has no audio, so it cannot be muted."
NOT_DONE = "OBS could not carry out the action."

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _label(name: str, fallback: str) -> str:
    text = " ".join(_CONTROL.sub(" ", name).split())
    if not text:
        return fallback
    return text if len(text) <= LABEL_MAX else text[:LABEL_MAX - 1] + "…"


def _uuid(data: dict, key: str) -> str:
    value = data.get(key)
    if value is None:
        raise RequiredFeaturesMissing()
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ProtocolFailure()
    return value


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ProtocolFailure()
    return value


def _flag(data: dict, key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise ProtocolFailure()
    return value


@dataclass
class _Scene:
    uuid: str
    name: str
    order: int
    program: bool = False
    revision: int = 0
    present: bool = True

    @property
    def object_id(self) -> str:
        return SCENE_PREFIX + self.uuid


@dataclass
class _Input:
    uuid: str
    name: str
    audio: bool | None                # None: this OBS version does not say
    muted: bool | None = None         # None until OBS has told us
    revision: int = 0
    present: bool = True
    mute_epoch: int | None = None      # the collection/load that established the mute value

    @property
    def object_id(self) -> str:
        return INPUT_PREFIX + self.uuid


class HostChannel:
    """Adapt the real SDK connection without detaching action sends."""

    def __init__(self, websocket):
        self.websocket = websocket

    @property
    def close_code(self):
        return self.websocket.close_code

    async def receive(self):
        try:
            return await self.websocket.receive()
        except WebSocketClosed:
            return None

    async def send(self, text):
        # Keep HostCallError intact: admission refusal is definitely unsent;
        # its outcome_unknown distinguishes submission failures correctly.
        await self.websocket.send(text)

    async def close(self):
        await self.websocket.close()


class ObsProvider(Provider):
    def __init__(self, connect: Connector | None = None):
        self._connect = connect
        self._setup: Setup | None = None
        self._connection: ObsConnection | None = None
        self._scenes: dict[str, _Scene] = {}
        self._inputs: dict[str, _Input] = {}
        self._caps = False                    # GetInputList reports inputKindCaps
        self._buffer: list[tuple[str, dict]] | None = None   # events during a load
        self._buffer_bytes = 0
        self._switching = False               # a scene collection change is under way
        self._reloading = False
        self._reload_again = False
        self._epoch = 0                       # invalidates outstanding collection/load reads
        self._epoch_changed = asyncio.Event()
        self._collection_changing = False
        self._too_many = False
        self._catalog = {SCENES: 0, INPUTS: 0}   # listing versions: move on add/remove/rename
        self._announce_pending: set[str] = set()  # objects.changed not yet published
        self._mutes_wanted = asyncio.Event()
        self._exiting = False                 # OBS announced it is closing
        self._stopping = False
        self._ready = False
        self.reports: list[str] = []          # unavailability reports made (codes only)

    # ── lifecycle ──

    async def initialize(self, setup: Setup) -> None:
        self._setup = setup
        try:
            channel = (HostChannel(await setup.host.open_websocket("obs"))
                       if self._connect is None else
                       await self._connect(setup.config["host"], setup.config["port"]))
        except HostCallError as error:
            if error.code == ErrorCode.INSTANCE_UNAVAILABLE:
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, UNREACHABLE,
                                       retryable=error.info.retryable) from None
            raise
        except IntegrationError:
            raise
        except (OSError, ConnectionError):
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, UNREACHABLE,
                                   retryable=True) from None
        connection = ObsConnection(channel, self._on_event)
        self._connection = connection
        try:
            hello = await connection.handshake(self._password)
            if hello.websocket_version < CAPS_VERSION:
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, TOO_OLD)
            self._caps = hello.websocket_version >= CAPS_VERSION
            setup.background.start(self._read, name="obs-reader")
            await self._load()
            await self._resolve_mutes()
            if any(i.present and i.muted is None for i in self._inputs.values()):
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, NOT_READY_YET)
            if self._switching or connection.closed:
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE,
                                       SWITCHING if self._switching else LOST, retryable=True)
            setup.background.start(self._mute_reader, name="obs-mutes")
            self._publish({s.object_id for s in self._scenes.values()} |
                          {i.object_id for i in self._inputs.values()})
        except (Closed, ProtocolFailure) as error:
            await connection.close()
            if isinstance(error, RequiredFeaturesMissing):
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, TOO_OLD) from None
            if self._too_many:
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, TOO_MANY) from None
            if isinstance(error, ProtocolFailure) or connection.failed:
                raise IntegrationError(ErrorCode.PROVIDER_ERROR, UNEXPECTED) from None
            raise _closed_error(error, starting=not connection.identified) from None
        except (RequestFailed, Busy) as error:
            await connection.close()
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE,
                                   SWITCHING if self._switching else NOT_READY_YET,
                                   retryable=isinstance(error, RequestFailed)
                                   and error.status == STATUS_NOT_READY) from None
        except BaseException:
            await connection.close()
            raise
        self._ready = True

    async def shutdown(self) -> None:
        self._stopping = True
        if self._connection is not None:
            await self._connection.close()

    async def _password(self) -> str:
        try:
            return await self._setup.host.read_secret(PASSWORD_FIELD)
        except HostCallError as error:
            if error.code in (ErrorCode.NEEDS_SETUP, ErrorCode.PERMISSION_DENIED):
                raise IntegrationError(ErrorCode.NEEDS_SETUP, PASSWORD_NEEDED) from None
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, PASSWORD_UNREADABLE) from None
        except IntegrationError:                       # HostCallTimeout and the like
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, PASSWORD_UNREADABLE) from None

    async def _read(self) -> None:
        """The one reader: runs until the connection ends, then reports it."""
        connection = self._connection
        try:
            reason = await connection.run()
        except Exception:                              # a defect in event handling
            await connection.close()
            reason = None
        if self._stopping:
            return
        message = (TOO_MANY if self._too_many
                   else UNEXPECTED if reason is None or connection.failed
                   else EXITED if self._exiting
                   else ENDED if reason.code == CLOSE_SESSION_INVALIDATED else LOST)
        self._report(ErrorCode.INSTANCE_UNAVAILABLE, message,
                     retryable=reason is not None and not connection.failed
                     and not self._too_many and _transient_close(reason.code))

    def _report(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        try:
            self._setup.events.report_unavailable(code, message, retryable=retryable)
            self.reports.append(code.value)
        except NotReadyError:
            pass                              # already stopping/closed

    # ── loading ──

    def _advance_epoch(self) -> None:
        self._epoch_changed.set()
        self._epoch_changed = asyncio.Event()
        self._epoch += 1

    async def _load(self) -> int | None:
        """Replace the model with OBS's current scenes and inputs.

        Every event received from the moment the lists are requested is held
        and applied on top of them, in order, so a change that raced the load
        is never lost or undone. Objects missing from the lists stay known as
        absent, so their revisions never go backwards in this session.
        """
        connection = self._connection
        self._advance_epoch()
        epoch = self._epoch
        self._buffer = []
        self._buffer_bytes = 0
        try:
            scene_list = await connection.request("GetSceneList")
            input_list = await connection.request("GetInputList")
            if epoch != self._epoch:
                return None                   # replies belong to a superseded collection/load
            scenes = scene_list.get("scenes")
            inputs = input_list.get("inputs")
            program = scene_list.get("currentProgramSceneUuid")
            if not isinstance(scenes, list) or not isinstance(inputs, list) \
                    or not (program is None or isinstance(program, str)):
                raise ProtocolFailure()
            changed = self._apply_lists(scenes, inputs, program)
            buffered, self._buffer = self._buffer, None
            for event_type, data in buffered:
                changed.update(self._apply(event_type, data))
        finally:
            self._buffer = None
        self._publish(changed)
        self._announce()
        self._mutes_wanted.set()
        return epoch

    def _apply_lists(self, scenes: list, inputs: list, program: str | None) -> set:
        seen_scenes, seen_inputs, changed = set(), set(), set()
        for entry in scenes:
            if not isinstance(entry, dict) or type(entry.get("sceneIndex")) is not int:
                raise ProtocolFailure()
            uuid = _uuid(entry, "sceneUuid")
            seen_scenes.add(uuid)
            scene = self._scene(uuid, _text(entry, "sceneName"))
            if scene.order != -entry["sceneIndex"]:
                scene.order = -entry["sceneIndex"]      # OBS's highest index is at the top
                self._catalog_changed(SCENES)
            changed |= self._set_scene(scene, present=True, program=uuid == program)
        for entry in inputs:
            if not isinstance(entry, dict):
                raise ProtocolFailure()
            caps = entry.get("inputKindCaps")
            if type(caps) is not int:
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, TOO_OLD)
            audio = bool(caps & OBS_SOURCE_AUDIO) if self._caps else None
            if audio is False:
                continue
            uuid = _uuid(entry, "inputUuid")
            seen_inputs.add(uuid)
            item = self._input(uuid, _text(entry, "inputName"), audio)
            item.mute_epoch = None             # every full load needs a current mute observation
            changed |= self._set_input(item, present=True)
        for scene in self._scenes.values():
            if scene.uuid not in seen_scenes:
                changed |= self._set_scene(scene, present=False, program=False)
        for item in self._inputs.values():
            if item.uuid not in seen_inputs:
                changed |= self._set_input(item, present=False)
        return changed

    async def _resolve_mutes(self) -> None:
        """Read every present input not yet observed in this collection/load.

        An event that arrives while a read is outstanding is at least as new
        as the read, so a read only fills a state not yet observed in this epoch.
        """
        self._mutes_wanted.clear()
        epoch = self._epoch
        epoch_changed = self._epoch_changed
        wanted = [item for item in self._inputs.values() if item.present and item.mute_epoch != epoch]
        gate = asyncio.Semaphore(MUTE_READS_AT_ONCE)

        async def read(item: _Input) -> None:
            async with gate:
                if epoch != self._epoch:
                    return
                try:
                    data = await self._connection.request("GetInputMute",
                                                          {"inputUuid": item.uuid})
                except RequestFailed:
                    return                 # removed meanwhile; its removal event follows
                if epoch != self._epoch or not item.present:
                    return                 # a late answer cannot populate a replacement model
                if type(data.get("inputMuted")) is not bool:
                    raise ProtocolFailure()
                if item.mute_epoch != epoch:  # a newer event in this epoch wins, even if unchanged
                    item.mute_epoch = epoch
                    if item.muted != data["inputMuted"]:
                        item.muted = data["inputMuted"]
                        item.revision += 1
                        self._publish({item.object_id})
        snapshot = asyncio.gather(*(read(item) for item in wanted))
        invalidated = asyncio.create_task(epoch_changed.wait())
        try:
            await asyncio.wait((snapshot, invalidated), return_when=asyncio.FIRST_COMPLETED)
            if epoch != self._epoch:
                return                       # finally abandons obsolete requests; late replies are ignored
            await snapshot
        finally:
            snapshot.cancel()
            invalidated.cancel()
            await asyncio.gather(snapshot, invalidated, return_exceptions=True)

    async def _mute_reader(self) -> None:
        """Background: state for inputs created after Ready."""
        while True:
            await self._mutes_wanted.wait()
            if self._switching:
                self._mutes_wanted.clear()   # the collection reloader owns its required snapshot
                continue
            try:
                await self._resolve_mutes()
            except (Closed, Busy, ProtocolFailure):
                await self._connection.close()
                return                     # the reader reports the connection's end

    async def _reload_collection(self) -> None:
        """Background: a new scene collection; replace the model, then accept actions."""
        try:
            while True:
                self._reload_again = False
                epoch = await self._load()
                if epoch is not None:
                    await self._resolve_mutes()
                if self._reload_again:
                    continue
                if self._collection_changing:
                    return                 # Changed will start the next load
                if epoch is None or epoch != self._epoch:
                    continue
                if any(i.present and i.mute_epoch != epoch for i in self._inputs.values()):
                    raise ProtocolFailure() # incomplete snapshot must never admit actions
                self._switching = False
                self._publish({s.object_id for s in self._scenes.values()} |
                              {i.object_id for i in self._inputs.values()})
                self._announce()            # the new collection's scenes and inputs
                break
        except (Closed, ProtocolFailure, RequestFailed, Busy, IntegrationError):
            await self._connection.close()          # the reader then reports it
        finally:
            self._reloading = False

    # ── events ──

    def _on_event(self, event_type: str, data: dict) -> None:
        if event_type == "ExitStarted":
            self._exiting = True
        elif event_type == "CurrentSceneCollectionChanging":
            self._advance_epoch()
            self._collection_changing = True
            self._switching = True
            self._catalog_changed(SCENES)
            self._catalog_changed(INPUTS)
        elif event_type == "CurrentSceneCollectionChanged":
            self._advance_epoch()
            self._collection_changing = False
            self._switching = True
            if self._ready and not self._stopping:
                if self._reloading:
                    self._reload_again = True
                else:
                    self._reloading = True
                    try:
                        self._setup.background.start(self._reload_collection,
                                                     name="obs-reload")
                    except IntegrationError:
                        self._reloading = False
                        raise ProtocolFailure() from None    # cannot follow OBS any more
        elif self._collection_changing:
            return                         # undefined old-collection events; reload after Changed
        elif self._buffer is not None:
            size = len(json.dumps((event_type, data), ensure_ascii=False).encode("utf-8"))
            if (len(self._buffer) >= BUFFERED_EVENTS_MAX or
                    self._buffer_bytes + size > BUFFERED_BYTES_MAX):
                raise ProtocolFailure()
            self._buffer_bytes += size
            self._buffer.append((event_type, data))
        else:
            self._publish(self._apply(event_type, data))
            self._announce()

    def _apply(self, event_type: str, data: dict) -> set:
        """Apply one model event; returns the object IDs whose state changed."""
        if event_type in ("SceneCreated", "SceneRemoved"):
            if _flag(data, "isGroup"):
                return set()
            scene = self._scene(_uuid(data, "sceneUuid"), _text(data, "sceneName"))
            if event_type == "SceneCreated":
                return self._set_scene(scene, present=True, program=scene.program)
            return self._set_scene(scene, present=False, program=False)
        if event_type == "SceneNameChanged":
            uuid = _uuid(data, "sceneUuid")
            if uuid in self._scenes:
                self._rename(self._scenes[uuid], _text(data, "sceneName"), SCENES)
            return set()
        if event_type == "CurrentProgramSceneChanged":
            uuid, changed = _uuid(data, "sceneUuid"), set()
            for scene in self._scenes.values():
                if scene.program and scene.uuid != uuid:
                    changed |= self._set_scene(scene, present=scene.present, program=False)
            if uuid in self._scenes:
                scene = self._scenes[uuid]
                changed |= self._set_scene(scene, present=scene.present, program=True)
            return changed
        if event_type == "InputCreated":
            caps = data.get("inputKindCaps")
            if type(caps) is not int:
                raise ProtocolFailure()
            audio = bool(caps & OBS_SOURCE_AUDIO) if type(caps) is int else None
            if audio is False:
                return set()
            item = self._input(_uuid(data, "inputUuid"), _text(data, "inputName"), audio)
            item.mute_epoch = None
            changed = self._set_input(item, present=True)
            self._mutes_wanted.set()
            return changed
        if event_type == "InputRemoved":
            uuid = _uuid(data, "inputUuid")
            return self._set_input(self._inputs[uuid], present=False) if uuid in self._inputs \
                else set()
        if event_type == "InputNameChanged":
            uuid = _uuid(data, "inputUuid")
            if uuid in self._inputs:
                self._rename(self._inputs[uuid], _text(data, "inputName"), INPUTS)
            return set()
        if event_type == "InputMuteStateChanged":
            uuid, muted = _uuid(data, "inputUuid"), _flag(data, "inputMuted")
            item = self._inputs.get(uuid)
            if item is None or not item.present:
                return set()
            item.mute_epoch = self._epoch
            if item.muted == muted:
                return set()
            item.muted = muted
            item.revision += 1
            return {item.object_id}
        return set()                    # other subscribed events are not used

    def _scene(self, uuid: str, name: str) -> _Scene:
        scene = self._scenes.get(uuid)
        if scene is None:
            self._check_room()
            order = max((s.order for s in self._scenes.values()), default=0) + 1
            scene = self._scenes[uuid] = _Scene(uuid, name, order, present=False)
        self._rename(scene, name, SCENES)
        return scene

    def _input(self, uuid: str, name: str, audio: bool | None) -> _Input:
        item = self._inputs.get(uuid)
        if item is None:
            self._check_room()
            item = self._inputs[uuid] = _Input(uuid, name, audio, present=False)
        self._rename(item, name, INPUTS)
        if audio is not None:
            item.audio = audio
        return item

    def _rename(self, item, name: str, collection_id: str) -> None:
        if item.name != name:
            item.name = name
            if item.present:
                self._catalog_changed(collection_id)

    def _catalog_changed(self, collection_id: str) -> None:
        """Listed scenes or inputs changed: invalidate listings and tell MIDImap."""
        self._catalog[collection_id] += 1
        self._announce_pending.add(collection_id)

    def _announce(self) -> None:
        if not self._ready:
            # MIDImap lists nothing of this session before Ready, and every
            # change so far is already in the model it will list.
            self._announce_pending.clear()
            return
        if self._stopping or self._switching:
            return                     # a collection change announces once it is complete
        for collection_id in sorted(self._announce_pending):
            try:
                self._setup.events.publish_objects_changed(collection_id)
            except EventQueueFull:
                if not self._ready:
                    raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, TOO_MANY_CHANGES) from None
                self._report(ErrorCode.INSTANCE_UNAVAILABLE, TOO_MANY_CHANGES)
                return
            except NotReadyError:
                return                 # stopping or closed
            self._announce_pending.discard(collection_id)

    def _check_room(self) -> None:
        if len(self._scenes) + len(self._inputs) >= KNOWN_MAX:
            self._too_many = True
            raise ProtocolFailure()

    def _set_scene(self, scene: _Scene, *, present: bool, program: bool) -> set:
        if (scene.present, scene.program) == (present, program):
            return set()
        if scene.present != present:
            self._catalog_changed(SCENES)
        scene.present, scene.program = present, program
        scene.revision += 1
        return {scene.object_id}

    def _set_input(self, item: _Input, *, present: bool) -> set:
        if item.present == present:
            return set()
        self._catalog_changed(INPUTS)
        item.present = present
        item.revision += 1
        return {item.object_id}

    def _publish(self, object_ids: set) -> None:
        if self._stopping or self._switching:
            return
        for object_id in sorted(object_ids):
            state = self._state(object_id)
            if state is None:
                continue
            try:
                self._setup.events.publish_state(state)
            except EventQueueFull:
                if not self._ready:
                    raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, TOO_MANY_CHANGES) from None
                self._report(ErrorCode.INSTANCE_UNAVAILABLE, TOO_MANY_CHANGES)
                return

    # ── discovery and state ──

    async def list_objects(self, collection_id: str, cursor: str | None) -> Page:
        """One page of the current listing. Every page of a scan comes from one
        catalog version (the cursor carries it), or the scan must restart."""
        self._check_usable()
        version = self._catalog[collection_id]
        if collection_id == SCENES:
            objects = [ObjectInfo(s.object_id, SCENES, _label(s.name, "Scene"), None,
                                  ("switch_scene",))
                       for s in sorted(self._scenes.values(), key=lambda s: s.order)
                       if s.present]
        else:
            objects = [ObjectInfo(i.object_id, INPUTS, _label(i.name, "Input"), None,
                                  ("set_input_mute",))
                       for i in self._inputs.values() if i.present]
        start = 0
        if cursor is not None:
            match = re.fullmatch(r"([0-9]{1,9})\.([0-9]{1,6})", cursor)
            if match is None or int(match[1]) != version or int(match[2]) > len(objects):
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, LIST_CHANGED,
                                       retryable=True)
            start = int(match[2])
        end = start + PAGE_SIZE
        return Page(objects[start:end], f"{version}.{end}" if end < len(objects) else None)

    async def read_states(self, object_ids: Sequence[str]) -> list[State]:
        """Answer from the event-maintained model, only while it is current.

        The one reader is connected, the model was loaded completely and is
        not being replaced (no scene collection change), and no event was
        lost: a full event buffer or a malformed event closes the
        connection. An unchanged object keeps its revision, so MIDImap can
        renew its freshness without a fabricated change; an input whose mute
        state is not yet known is left out rather than guessed.
        """
        self._check_usable()
        if not self._ready:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, NOT_CONNECTED, retryable=True)
        return [state for state in map(self._state, object_ids) if state is not None]

    def _state(self, object_id: str) -> State | None:
        if object_id.startswith(SCENE_PREFIX):
            scene = self._scenes.get(object_id[len(SCENE_PREFIX):])
            if scene is not None:
                return State(object_id, SCENES, scene.revision, {"program": scene.program},
                             available=scene.present)
        elif object_id.startswith(INPUT_PREFIX):
            item = self._inputs.get(object_id[len(INPUT_PREFIX):])
            if item is not None and item.muted is not None:
                return State(object_id, INPUTS, item.revision, {"muted": item.muted},
                             available=item.present)
        return None

    # ── actions ──

    @action("switch_scene")
    async def switch_scene(self, arguments: dict) -> dict:
        self._check_usable()
        scene = self._scenes.get(_target(arguments["object_id"], SCENE_PREFIX))
        if scene is None or not scene.present:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_SCENE)
        await self._send("SetCurrentProgramScene", {"sceneUuid": scene.uuid}, MISSING_SCENE)
        return {"object_id": arguments["object_id"]}

    @action("set_input_mute")
    async def set_input_mute(self, arguments: dict) -> dict:
        self._check_usable()
        item = self._inputs.get(_target(arguments["object_id"], INPUT_PREFIX))
        if item is None or not item.present:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_INPUT)
        if item.audio is False:
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, NO_AUDIO)
        await self._send("SetInputMute", {"inputUuid": item.uuid,
                                          "inputMuted": arguments["muted"]}, MISSING_INPUT)
        return {"object_id": arguments["object_id"], "muted": arguments["muted"]}

    def _check_usable(self) -> None:
        if self._connection is None or self._connection.closed or self._stopping:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, NOT_CONNECTED, retryable=True)
        if self._switching:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, SWITCHING, retryable=True)

    async def _send(self, request_type: str, data: dict, missing: str) -> None:
        """Send one action request once; the answer decides the outcome."""
        try:
            await self._connection.request(request_type, data)
        except RequestFailed as failed:
            raise _request_error(failed.status, missing) from None
        except Closed as closed:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE,
                                   LOST_DURING_ACTION if closed.sent else NOT_CONNECTED,
                                   retryable=True, outcome_unknown=closed.sent) from None
        except Busy:
            raise IntegrationError(ErrorCode.SATURATED, BUSY, retryable=True) from None


def _target(object_id: str, prefix: str) -> str | None:
    return object_id[len(prefix):] if object_id.startswith(prefix) else None


def _request_error(status: int, missing: str) -> IntegrationError:
    """An OBS failure status for SetCurrentProgramScene / SetInputMute.

    In obs-websocket 5.6.3 these requests return every failure status before
    changing anything, so a known status means no effect. Others (7xx, or a
    status this version does not know) are reported as uncertain.
    """
    if status in (STATUS_RESOURCE_NOT_FOUND, STATUS_INVALID_RESOURCE_TYPE):
        return IntegrationError(ErrorCode.TARGET_MISSING, missing)
    if status == STATUS_INVALID_RESOURCE_STATE:
        return IntegrationError(ErrorCode.INVALID_ARGUMENT, NO_AUDIO)
    if status == STATUS_NOT_READY:
        return IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, SWITCHING, retryable=True)
    return IntegrationError(ErrorCode.PROVIDER_ERROR, NOT_DONE,
                            outcome_unknown=not 200 <= status < 700)


def _closed_error(closed: Closed, *, starting: bool) -> IntegrationError:
    if closed.code == CLOSE_AUTHENTICATION_FAILED:
        return IntegrationError(ErrorCode.NEEDS_SETUP, PASSWORD_REFUSED)
    if closed.code == CLOSE_UNSUPPORTED_RPC_VERSION:
        return IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, UNSUPPORTED)
    return IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, UNREACHABLE if starting else LOST,
                            retryable=_transient_close(closed.code))


def _transient_close(code: int | None) -> bool:
    # Explicit allowlist. OBS 4011 is an operator Kick: never reconnect.
    # Unknown/4xxx, authentication, protocol, oversize and policy failures
    # require correction/manual Retry instead of silently restarting defects.
    return code in (None, 1000, 1001, 1006, 1011, 1012, 1013, 1014)
