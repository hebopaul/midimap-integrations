"""Everything a streamer drives in OBS beyond scenes and mute: outputs, studio
mode, volume, sources, filters, transitions, media, profiles, plus the toasts
and the MIDI menus.

`ObsControls` is mixed into `ObsProvider` and shares its model and connection.
The same guarantees hold: only UUIDs or stable digests identify a target, a
request is sent once and its answer decides the outcome, OBS text is never
shown, and every list is read in bounded batches.

Volumes, sources, filters and hotkeys are read on first use, not at start, so a
large scene collection cannot delay or fail Ready. Toggle state comes from events.

Menus keep no per-viewer state. Item and page IDs are digests of the object
they control, so the overlay and the in-app panel can be open at once.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass

from midimap_integration_sdk import ErrorCode, IntegrationError, ObjectInfo, State, action, toast_suppressed

from obs_protocol import (
    STATUS_INVALID_RESOURCE_STATE, STATUS_INVALID_RESOURCE_TYPE, STATUS_NOT_READY,
    STATUS_OUTPUT_DISABLED, STATUS_OUTPUT_NOT_PAUSED, STATUS_OUTPUT_NOT_RUNNING,
    STATUS_OUTPUT_PAUSED, STATUS_OUTPUT_RUNNING, STATUS_RESOURCE_NOT_FOUND,
    STATUS_STUDIO_MODE_NOT_ACTIVE, Busy, Closed, ProtocolFailure, RequestFailed,
)

log = logging.getLogger("obs")

OUTPUTS, ITEMS, FILTERS = "outputs", "scene_items", "filters"
TRANSITIONS, MEDIA, SCENE_COLLECTIONS, PROFILES, HOTKEYS = (
    "transitions", "media_inputs", "scene_collections", "profiles", "hotkeys")
STREAM, RECORD, REPLAY, VIRTUALCAM, STUDIO = "stream", "record", "replay", "virtualcam", "studio"
MEDIA_KINDS = frozenset({"ffmpeg_source", "vlc_source"})
QUERIES_AT_ONCE = 6                    # leave room for the reader in the 8 host-call slots
MENU_ITEMS_MAX = 200                   # host limit per page
DETAIL_PAGES_MAX = 24                  # host allows 32 extra pages; the hub declares 7
MENU_PERMISSION, TOAST_PERMISSION = "obs.menu", "obs.toast"
TOAST_ID = "obs"
START_WATCH_S = 10                     # a slow encoder or stream connection can take several seconds

# Fixed messages. OBS text is never shown.
NOT_CONNECTED = "OBS is not connected."
LOST_DURING_ACTION = "The connection to OBS was lost; OBS may have carried out the action."
BUSY = "Too many OBS requests are waiting; try again."
NOT_DONE = "OBS could not carry out the action."
SWITCHING = "OBS is switching scene collections; try again in a moment."
MISSING_ITEM = "The selected source is no longer in that scene."
MISSING_FILTER = "The selected filter no longer exists in OBS."
MISSING_SCENE = "The selected scene no longer exists in OBS."
MISSING_INPUT = "The selected input no longer exists in OBS."
MISSING_TRANSITION = "The selected transition no longer exists in OBS."
MISSING_MEDIA = "The selected media source no longer exists in OBS."
MISSING_NAMED = "The selected item no longer exists in OBS."
NO_REPLAY = "The replay buffer is off in OBS. Turn it on under Settings, Output."
NO_VIRTUALCAM = "The virtual camera is not available in this OBS."
REPLAY_OFF = "The replay buffer is not running, so there is no clip to save."
NOT_RECORDING = "OBS is not recording."
NOT_PAUSED = "The recording is not paused."
NO_CHAPTERS = "Chapters need an output format that supports them, such as Hybrid MP4."
NEEDS_STUDIO = "Studio mode is off in OBS."
NO_SCENES = "OBS has no other scene to switch to."
VOLUME_UNKNOWN = "OBS has not reported this input's volume yet; try again."
TOO_MANY = "OBS has more sources and filters than this integration supports."
BAD_FOLDER = "OBS could not save there. Use a full folder path on the OBS computer."
BAD_MENU = "Unknown OBS menu."
DID_NOT_START = "Didn't start. Check OBS for a message."

SCENE_ON_AIR = "Put it on air"
SCENE_TWICE = "Press twice to put it on air"
INPUT_TOGGLE = "Toggle mute"
SOURCE_TOGGLE = "Show or hide it"
CLIP = "clip"
QUIET_S = 15                           # a suppressed start or stop may take this long to report
TOAST_TOPICS = {
    "ToggleStream": STREAM, "StartStream": STREAM, "StopStream": STREAM,
    "ToggleRecord": RECORD, "StartRecord": RECORD, "StopRecord": RECORD,
    "ToggleRecordPause": RECORD, "PauseRecord": RECORD, "ResumeRecord": RECORD,
    "ToggleReplayBuffer": REPLAY, "StartReplayBuffer": REPLAY, "StopReplayBuffer": REPLAY,
    "SaveReplayBuffer": CLIP,
    "ToggleVirtualCam": VIRTUALCAM, "StartVirtualCam": VIRTUALCAM, "StopVirtualCam": VIRTUALCAM,
    "SetStudioModeEnabled": STUDIO,
}
OUTPUT_LABELS = {STREAM: "Streaming", RECORD: "Recording", REPLAY: "Replay buffer",
                 VIRTUALCAM: "Virtual camera", STUDIO: "Studio mode"}
OUTPUT_STATUS = {"GetStreamStatus": STREAM, "GetRecordStatus": RECORD,
                 "GetReplayBufferStatus": REPLAY, "GetVirtualCamStatus": VIRTUALCAM}
OUTPUT_EVENTS = {"StreamStateChanged": STREAM, "RecordStateChanged": RECORD,
                 "ReplayBufferStateChanged": REPLAY, "VirtualcamStateChanged": VIRTUALCAM}
_STATE_WORDS = {
    "OBS_WEBSOCKET_OUTPUT_STARTED": ("Started", "success"),
    "OBS_WEBSOCKET_OUTPUT_STOPPED": ("Stopped", "muted"),
    "OBS_WEBSOCKET_OUTPUT_PAUSED": ("Paused", "warning"),
    "OBS_WEBSOCKET_OUTPUT_RESUMED": ("Resumed", "success"),
    "OBS_WEBSOCKET_OUTPUT_RECONNECTING": ("Reconnecting", "warning"),
    "OBS_WEBSOCKET_OUTPUT_RECONNECTED": ("Reconnected", "success"),
}
_ACTIVE_STATES = {"OBS_WEBSOCKET_OUTPUT_STARTED": True, "OBS_WEBSOCKET_OUTPUT_RESUMED": True,
                  "OBS_WEBSOCKET_OUTPUT_RECONNECTED": True, "OBS_WEBSOCKET_OUTPUT_PAUSED": True,
                  "OBS_WEBSOCKET_OUTPUT_RECONNECTING": True, "OBS_WEBSOCKET_OUTPUT_STOPPED": False}
MEDIA_ACTIONS = {"play": "PLAY", "pause": "PAUSE", "restart": "RESTART", "stop": "STOP",
                 "next": "NEXT", "previous": "PREVIOUS"}
_MEDIA_PREFIX = "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
LABEL_MAX = 128

# OBS's own mixer curve (libobs/obs-audio-controls.c, OBS_FADER_LOG), so a MIDI
# fader at 50% puts OBS's slider at 50%.
_LOG_OFFSET_DB, _LOG_RANGE_DB = 6.0, 96.0
_LOG_OFFSET_VAL = -0.77815125038364363
_LOG_RANGE_VAL = -2.00860017176191756


def percent_to_mul(percent: float) -> float:
    """Fader position (0-100) to OBS's linear volume multiplier."""
    position = max(0.0, min(1.0, percent / 100))
    if position <= 0:
        return 0.0
    if position >= 1:
        return 1.0
    ratio = (_LOG_RANGE_DB + _LOG_OFFSET_DB) / _LOG_OFFSET_DB
    db = -(_LOG_RANGE_DB + _LOG_OFFSET_DB) * pow(ratio, -position) + _LOG_OFFSET_DB
    return pow(10, db / 20)


def mul_to_percent(mul: float) -> float:
    """OBS's multiplier to fader position, 0-100, to one decimal.

    The multiplier is used because OBS reports -infinity decibels as null.
    Values above 0 dB (OBS allows +26) read as 100.
    """
    if mul <= 0:
        return 0.0
    db = 20 * math.log10(mul)
    if db >= 0:
        return 100.0
    if db <= -_LOG_RANGE_DB:
        return 0.0
    position = (-math.log10(-db + _LOG_OFFSET_DB) - _LOG_RANGE_VAL) / (_LOG_OFFSET_VAL - _LOG_RANGE_VAL)
    return round(max(0.0, min(1.0, position)) * 100, 1)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def label_of(name: str, fallback: str) -> str:
    text = " ".join(_CONTROL.sub(" ", name).split())
    if not text:
        return fallback
    return text if len(text) <= LABEL_MAX else text[:LABEL_MAX - 1] + "…"


def _text_field(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ProtocolFailure()
    return value


def _flag_field(data: dict, key: str) -> bool:
    value = data.get(key)
    if type(value) is not bool:
        raise ProtocolFailure()
    return value


def _uuid_field(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ProtocolFailure()
    return value


def _number_field(data: dict, key: str) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProtocolFailure()
    return value


@dataclass
class _Output:
    key: str
    active: bool = False
    paused: bool = False
    reconnecting: bool = False
    available: bool = False            # OBS answered: the output exists and is configured
    revision: int = 0
    events: int = 0                    # a status read older than an event is dropped

    @property
    def object_id(self) -> str:
        return "output:" + self.key


@dataclass
class _Item:
    scene_uuid: str
    item_id: int
    name: str
    scene_name: str
    enabled: bool
    source_uuid: str
    revision: int = 0

    @property
    def object_id(self) -> str:
        return f"item:{self.scene_uuid}:{self.item_id}"


@dataclass
class _Filter:
    source_uuid: str
    source_name: str
    name: str
    enabled: bool
    revision: int = 0

    @property
    def object_id(self) -> str:
        return f"filter:{self.source_uuid}:{digest(self.name)}"


@dataclass
class _Named:
    """A transition, scene collection, profile or hotkey: OBS addresses these by name."""
    object_id: str
    name: str
    current: bool = False
    revision: int = 0


def _simple(action_id, request, *, idempotent=(), messages=None, needs_recording=False, starts=None):
    """An action that sends one fixed request.

    `idempotent` statuses mean OBS is already where the user asked, which is
    success: a macro's "Start recording" must not fail because it already runs.
    `needs_recording` refuses locally: OBS answers success to a pause request
    while nothing records, which would tell the user something false.
    `starts` names an output to watch: OBS accepts a start it then cannot do.
    """
    @action(action_id)
    async def handler(self, arguments: dict) -> dict:
        self._check_usable()
        if needs_recording and not self._outputs[RECORD].active:
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, NOT_RECORDING)
        was_active = None if starts is None else self._outputs[starts].active
        answer = await self._control(request, None, messages or {}, idempotent)
        result = {"accepted": True}
        if type(answer.get("outputActive")) is bool:
            result["active"] = answer["outputActive"]
        if starts is not None and not was_active and result.get("active", True):
            self._watch_start(starts)
        return result
    return handler


REPLAY_MESSAGES = {STATUS_INVALID_RESOURCE_STATE: NO_REPLAY, STATUS_OUTPUT_DISABLED: NO_REPLAY}
VIRTUALCAM_MESSAGES = {STATUS_INVALID_RESOURCE_STATE: NO_VIRTUALCAM, STATUS_OUTPUT_DISABLED: NO_VIRTUALCAM}
RECORD_PAUSE_MESSAGES = {STATUS_OUTPUT_NOT_RUNNING: NOT_RECORDING, STATUS_OUTPUT_NOT_PAUSED: NOT_PAUSED}


class ObsControls:
    # ── state ──

    def _controls_init(self) -> None:
        self._quiet_until: dict[str, float] = {}
        self._menu_options: dict[str, dict] = {}
        self._outputs = {key: _Output(key) for key in OUTPUT_LABELS}
        self._items: dict[str, _Item] = {}
        self._filters: dict[str, _Filter] = {}
        self._transitions: dict[str, _Named] = {}
        self._transition_duration: int | None = None
        self._scene_collections: dict[str, _Named] = {}
        self._profiles: dict[str, _Named] = {}
        self._hotkeys: dict[str, _Named] = {}
        self._sources: dict[str, str] = {}     # every input's and scene's UUID -> name
        self._preview: str | None = None       # the preview scene's UUID in studio mode
        self._items_epoch = self._filters_epoch = None   # the epoch each lazy list was read in
        self._names_stale = True
        self._lazy_lock = asyncio.Lock()
        self._revisions = 0
        self._menu_refs: dict[str, str] = {}   # item/page digest -> object ID
        self._pending_toast: tuple | None = None
        self._toast_wanted = asyncio.Event()
        self._toast_sequence = 0

    def _next_revision(self) -> int:
        self._revisions += 1
        return self._revisions

    # ── requests ──

    async def _query(self, request: str, data: dict | None = None) -> dict:
        """A read. Failures become fixed, retryable messages; nothing here changes OBS."""
        try:
            return await self._connection.request(request, data)
        except Closed:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, NOT_CONNECTED, retryable=True) from None
        except Busy:
            raise IntegrationError(ErrorCode.SATURATED, BUSY, retryable=True) from None

    async def _control(self, request: str, data: dict | None, messages: dict, idempotent=()) -> dict:
        """A write, sent once. The answer decides the outcome; uncertainty is reported, never retried."""
        if request in TOAST_TOPICS and toast_suppressed():
            self._quiet_until[TOAST_TOPICS[request]] = time.monotonic() + QUIET_S   # the event may beat the answer
        try:
            return await self._connection.request(request, data)
        except RequestFailed as failed:
            if failed.status in idempotent:
                return {}
            log.warning("OBS refused %s with status %s", request, failed.status)
            raise self._control_error(failed.status, messages) from None
        except Closed as closed:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE,
                                   LOST_DURING_ACTION if closed.sent else NOT_CONNECTED,
                                   retryable=True, outcome_unknown=closed.sent) from None
        except Busy:
            raise IntegrationError(ErrorCode.SATURATED, BUSY, retryable=True) from None

    @staticmethod
    def _control_error(status: int, messages: dict) -> IntegrationError:
        if status in messages:
            code = (ErrorCode.TARGET_MISSING if status in (STATUS_RESOURCE_NOT_FOUND, STATUS_INVALID_RESOURCE_TYPE)
                    else ErrorCode.INVALID_ARGUMENT)
            return IntegrationError(code, messages[status])
        if status == STATUS_NOT_READY:
            return IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, SWITCHING, retryable=True)
        # In 5.6.3 every 2xx-6xx failure returns before changing anything.
        return IntegrationError(ErrorCode.PROVIDER_ERROR, NOT_DONE, outcome_unknown=not 200 <= status < 700)

    # ── loading ──

    async def _load_extras(self) -> None:
        """Outputs, studio mode, preview, transitions, collections and profiles.

        An output OBS refuses to describe (a disabled replay buffer, say) is
        marked unavailable; only a lost connection stops the load.
        """
        for request, key in OUTPUT_STATUS.items():
            output = self._outputs[key]
            seen = output.events
            try:
                data = await self._connection.request(request)
            except RequestFailed:
                data = None
            if output.events != seen:
                continue                       # an event is newer than this answer
            self._set_output(output, available=data is not None,
                             active=data is not None and data.get("outputActive") is True,
                             paused=data is not None and data.get("outputPaused") is True,
                             reconnecting=data is not None and data.get("outputReconnecting") is True)
        studio = self._outputs[STUDIO]
        seen = studio.events
        try:
            data = await self._connection.request("GetStudioModeEnabled")
        except RequestFailed:
            data = None
        if studio.events == seen:
            self._set_output(studio, available=data is not None,
                             active=data is not None and data.get("studioModeEnabled") is True)
        await self._read_preview()
        await self._read_transitions()
        await self._read_names()

    async def _read_preview(self) -> None:
        self._preview = None
        if self._outputs[STUDIO].active:
            try:
                data = await self._connection.request("GetCurrentPreviewScene")
            except RequestFailed:
                return
            preview = data.get("currentPreviewSceneUuid", data.get("sceneUuid"))
            self._preview = preview if isinstance(preview, str) else None

    async def _read_transitions(self) -> None:
        try:
            data = await self._connection.request("GetSceneTransitionList")
        except RequestFailed:
            return
        entries = data.get("transitions")
        current = data.get("currentSceneTransitionName")
        if not isinstance(entries, list):
            raise ProtocolFailure()
        names = [_text_field(entry, "transitionName") for entry in entries if isinstance(entry, dict)]
        self._replace_named(self._transitions, "transition:", TRANSITIONS, names, current)
        try:
            duration = await self._connection.request("GetCurrentSceneTransition")
        except RequestFailed:
            return
        value = duration.get("transitionDuration")
        self._transition_duration = value if type(value) is int else None

    async def _read_names(self) -> None:
        self._names_stale = False
        for request, list_key, current_key, store, prefix, collection in (
                ("GetSceneCollectionList", "sceneCollections", "currentSceneCollectionName",
                 self._scene_collections, "collection:", SCENE_COLLECTIONS),
                ("GetProfileList", "profiles", "currentProfileName", self._profiles, "profile:", PROFILES)):
            try:
                data = await self._connection.request(request)
            except RequestFailed:
                continue
            names = data.get(list_key)
            if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                raise ProtocolFailure()
            self._replace_named(store, prefix, collection, names, data.get(current_key))

    def _replace_named(self, store: dict, prefix: str, collection: str, names: list, current) -> None:
        wanted = {prefix + digest(name): name for name in names}
        if set(wanted) != set(store) or any(store[key].name != name for key, name in wanted.items()):
            self._catalog_changed(collection)
        for key in list(store):
            if key not in wanted:
                del store[key]
        changed = set()
        for key, name in wanted.items():
            named = store.get(key)
            if named is None:
                named = store[key] = _Named(key, name, revision=self._next_revision())
            is_current = name == current
            if named.current != is_current:
                named.current = is_current
                named.revision = self._next_revision()
                changed.add(key)
        self._publish_extras(changed)

    def _publish_extras(self, object_ids: set) -> None:
        """Before Ready these wait to be read. The SDK's start-up publication bound
        (256 objects) belongs to scenes and inputs, so a large collection can connect."""
        if self._ready:
            self._publish(object_ids)

    def _set_current_named(self, store: dict, name: str) -> set:
        changed = set()
        for key, named in store.items():
            is_current = named.name == name
            if named.current != is_current:
                named.current = is_current
                named.revision = self._next_revision()
                changed.add(key)
        return changed

    def _set_output(self, output: _Output, **fields) -> set:
        before = (output.available, output.active, output.paused, output.reconnecting)
        for name, value in fields.items():
            setattr(output, name, value)
        if before == (output.available, output.active, output.paused, output.reconnecting):
            return set()
        output.revision = self._next_revision()
        changed = {output.object_id}
        self._publish_extras(changed)
        return changed

    def _forget_collection(self) -> None:
        """A different scene collection: sources and filters must be read again."""
        self._items.clear()
        self._filters.clear()
        self._items_epoch = self._filters_epoch = None
        self._names_stale = True

    async def _gathered(self, calls: list) -> None:
        """Run zero-argument coroutine functions, a few at a time."""
        gate = asyncio.Semaphore(QUERIES_AT_ONCE)

        async def bounded(call):
            async with gate:
                await call()
        results = await asyncio.gather(*(bounded(call) for call in calls), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    @staticmethod
    def _lazy_max() -> int:
        return 4000

    async def _ensure_items(self) -> None:
        if self._items_epoch == self._epoch:
            return
        async with self._lazy_lock:
            if self._items_epoch == self._epoch:
                return
            epoch = self._epoch
            found: dict[str, _Item] = {}
            groups: list[tuple[str, str]] = []

            async def read(scene_uuid: str, scene_name: str, request: str) -> None:
                try:
                    data = await self._query(request, {"sceneUuid": scene_uuid})
                except RequestFailed:
                    return                     # removed meanwhile; its event follows
                entries = data.get("sceneItems")
                if not isinstance(entries, list):
                    raise ProtocolFailure()
                for entry in reversed(entries):  # OBS lists bottom first; its dock shows top first
                    if not isinstance(entry, dict) or type(entry.get("sceneItemId")) is not int:
                        raise ProtocolFailure()
                    item = _Item(scene_uuid, entry["sceneItemId"], _text_field(entry, "sourceName"), scene_name,
                                 _flag_field(entry, "sceneItemEnabled"), _uuid_field(entry, "sourceUuid"))
                    found[item.object_id] = item
                    if entry.get("isGroup") is True:
                        groups.append((item.source_uuid, item.name))

            try:
                await self._gathered([lambda s=s: read(s.uuid, s.name, "GetSceneItemList")
                                      for s in self._scenes.values() if s.present])
                await self._gathered([lambda g=g: read(g[0], g[1], "GetGroupSceneItemList") for g in groups])
            except ProtocolFailure:
                raise IntegrationError(ErrorCode.PROVIDER_ERROR, NOT_DONE) from None
            if epoch != self._epoch:
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, SWITCHING, retryable=True)
            if len(found) > self._lazy_max():
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, TOO_MANY)
            for key, item in found.items():
                old = self._items.get(key)
                item.revision = old.revision if old is not None and old.enabled == item.enabled \
                    else self._next_revision()
            self._items = found
            self._items_epoch = epoch

    async def _ensure_filters(self) -> None:
        if self._filters_epoch == self._epoch:
            return
        async with self._lazy_lock:
            if self._filters_epoch == self._epoch:
                return
            epoch = self._epoch
            found: dict[str, _Filter] = {}

            async def read(source_uuid: str, source_name: str) -> None:
                try:
                    data = await self._query("GetSourceFilterList", {"sourceUuid": source_uuid})
                except RequestFailed:
                    return
                entries = data.get("filters")
                if not isinstance(entries, list):
                    raise ProtocolFailure()
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ProtocolFailure()
                    item = _Filter(source_uuid, source_name, _text_field(entry, "filterName"),
                                   _flag_field(entry, "filterEnabled"))
                    found[item.object_id] = item

            try:
                await self._gathered([lambda u=uuid, n=name: read(u, n) for uuid, name in self._sources.items()])
            except ProtocolFailure:
                raise IntegrationError(ErrorCode.PROVIDER_ERROR, NOT_DONE) from None
            if epoch != self._epoch:
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, SWITCHING, retryable=True)
            if len(found) > self._lazy_max():
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, TOO_MANY)
            for key, item in found.items():
                old = self._filters.get(key)
                item.revision = old.revision if old is not None and old.enabled == item.enabled \
                    else self._next_revision()
            self._filters = found
            self._filters_epoch = epoch

    async def _ensure_volumes(self) -> None:
        """Read inputs whose volume isn't known in this collection. An event wins over a read."""
        epoch = self._epoch

        async def read(item) -> None:
            try:
                data = await self._query("GetInputVolume", {"inputUuid": item.uuid})
            except RequestFailed:
                return                         # the mixer shows the input without a slider
            mul = data.get("inputVolumeMul")
            if isinstance(mul, bool) or not isinstance(mul, (int, float)) or mul < 0:
                raise IntegrationError(ErrorCode.PROVIDER_ERROR, NOT_DONE)
            if epoch != self._epoch or not item.present or item.volume_epoch == epoch:
                return
            item.volume_epoch = epoch
            if item.volume != mul:
                item.volume = mul
                item.revision += 1
                self._publish({item.object_id})

        await self._gathered([lambda i=item: read(i) for item in self._inputs.values()
                              if item.present and item.volume_epoch != epoch])

    async def _ensure_hotkeys(self) -> None:
        if self._hotkeys:
            return
        try:
            data = await self._query("GetHotkeyList")
        except RequestFailed:
            return
        names = data.get("hotkeys")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, NOT_DONE)
        for name in names[:self._lazy_max()]:
            key = "hotkey:" + digest(name)
            self._hotkeys[key] = _Named(key, name, revision=self._next_revision())

    # ── events ──

    def _apply_controls(self, event_type: str, data: dict) -> set:
        """Apply one event this module owns; returns the object IDs whose state changed."""
        key = OUTPUT_EVENTS.get(event_type)
        if key is not None:
            return self._output_event(key, data)
        if event_type == "StudioModeStateChanged":
            output = self._outputs[STUDIO]
            output.events += 1
            enabled = _flag_field(data, "studioModeEnabled")
            self._preview = None
            if enabled and self._ready and not self._stopping:
                self._setup.background.start(self._preview_after_studio, name="obs-preview")
            self._note_toast(OUTPUT_LABELS[STUDIO], "On" if enabled else "Off", "success" if enabled else "muted",
                             topic=STUDIO)
            return self._set_output(output, available=True, active=enabled)
        if event_type == "ReplayBufferSaved":
            self._note_toast("Clip saved", "The replay was saved to disk.", "success", topic=CLIP)
            return set()
        if event_type == "CurrentPreviewSceneChanged":
            self._preview = _uuid_field(data, "sceneUuid")
            return set()
        if event_type == "InputVolumeChanged":
            item = self._inputs.get(_uuid_field(data, "inputUuid"))
            mul = _number_field(data, "inputVolumeMul")
            if item is None or not item.present:
                return set()
            item.volume_epoch = self._epoch
            if item.volume == mul:
                return set()
            item.volume = mul
            item.revision += 1
            return {item.object_id}
        if event_type in ("MediaInputPlaybackStarted", "MediaInputPlaybackEnded"):
            item = self._inputs.get(_uuid_field(data, "inputUuid"))
            playing = event_type == "MediaInputPlaybackStarted"
            if item is None or item.kind not in MEDIA_KINDS or item.playing == playing:
                return set()
            item.playing = playing
            item.media_revision = self._next_revision()
            return {"media:" + item.uuid}
        if event_type == "SceneItemEnableStateChanged":
            item = self._items.get(f"item:{_uuid_field(data, 'sceneUuid')}:{data.get('sceneItemId')}")
            enabled = _flag_field(data, "sceneItemEnabled")
            if item is None or item.enabled == enabled:
                return set()
            item.enabled = enabled
            item.revision = self._next_revision()
            return {item.object_id}
        if event_type in ("SceneItemCreated", "SceneItemRemoved"):
            self._items_epoch = None           # read again on next use: order and groups may differ
            self._catalog_changed(ITEMS)
            return set()
        if event_type in ("SourceFilterCreated", "SourceFilterRemoved", "SourceFilterNameChanged"):
            self._filters_epoch = None
            self._catalog_changed(FILTERS)
            return set()
        if event_type == "SourceFilterEnableStateChanged":
            source, name = _text_field(data, "sourceName"), _text_field(data, "filterName")
            enabled = _flag_field(data, "filterEnabled")
            changed = set()
            for item in self._filters.values():
                if item.source_name == source and item.name == name and item.enabled != enabled:
                    item.enabled = enabled
                    item.revision = self._next_revision()
                    changed.add(item.object_id)
            return changed
        if event_type == "CurrentSceneTransitionChanged":
            return self._set_current_named(self._transitions, _text_field(data, "transitionName"))
        if event_type == "CurrentSceneTransitionDurationChanged":
            value = data.get("transitionDuration")
            self._transition_duration = value if type(value) is int else None
            return set()
        if event_type == "CurrentProfileChanged":
            return self._set_current_named(self._profiles, _text_field(data, "profileName"))
        if event_type in ("SceneCollectionListChanged", "ProfileListChanged", "CurrentProfileChanging"):
            self._names_stale = True
            self._catalog_changed(SCENE_COLLECTIONS)
            self._catalog_changed(PROFILES)
            return set()
        return set()

    def _output_event(self, key: str, data: dict) -> set:
        output = self._outputs[key]
        output.events += 1
        _flag_field(data, "outputActive")      # obs-websocket reports false while paused or reconnecting
        state = _text_field(data, "outputState")
        word = _STATE_WORDS.get(state)
        active = _ACTIVE_STATES.get(state, output.active)
        paused = state == "OBS_WEBSOCKET_OUTPUT_PAUSED" or (output.paused and state not in (
            "OBS_WEBSOCKET_OUTPUT_RESUMED", "OBS_WEBSOCKET_OUTPUT_STOPPED", "OBS_WEBSOCKET_OUTPUT_STOPPING"))
        reconnecting = state == "OBS_WEBSOCKET_OUTPUT_RECONNECTING"
        if word is not None:
            # A connection problem isn't what the action asked for: always say so.
            connection = state in ("OBS_WEBSOCKET_OUTPUT_RECONNECTING", "OBS_WEBSOCKET_OUTPUT_RECONNECTED")
            self._note_toast(OUTPUT_LABELS[key], word[0], word[1], topic=None if connection else key)
        return self._set_output(output, available=True, active=active, paused=paused, reconnecting=reconnecting)

    # ── discovery and state ──

    def _listing(self, collection_id: str) -> list:
        """The objects of a collection this module owns."""
        if collection_id == OUTPUTS:
            return [ObjectInfo(o.object_id, OUTPUTS, OUTPUT_LABELS[o.key], None, ()) for o in self._outputs.values()]
        if collection_id == ITEMS:
            return [ObjectInfo(i.object_id, ITEMS, label_of(f"{i.scene_name} · {i.name}", "Source"), None,
                               ("set_source_visible", "toggle_source_visible")) for i in self._items.values()]
        if collection_id == FILTERS:
            return [ObjectInfo(f.object_id, FILTERS, label_of(f"{f.source_name} · {f.name}", "Filter"), None,
                               ("set_filter_enabled", "toggle_filter")) for f in self._filters.values()]
        if collection_id == MEDIA:
            return [ObjectInfo("media:" + i.uuid, MEDIA, label_of(i.name, "Media"), None,
                               ("media_control", "toggle_media_playback"))
                    for i in self._inputs.values() if i.present and i.kind in MEDIA_KINDS]
        store, capability = {TRANSITIONS: (self._transitions, "set_transition"),
                             SCENE_COLLECTIONS: (self._scene_collections, "switch_scene_collection"),
                             PROFILES: (self._profiles, "switch_profile"),
                             HOTKEYS: (self._hotkeys, "trigger_hotkey")}[collection_id]
        return [ObjectInfo(n.object_id, collection_id, label_of(n.name, "Item"), None, (capability,))
                for n in store.values()]

    async def _prepare_listing(self, collection_id: str) -> None:
        if collection_id == ITEMS:
            await self._ensure_items()
        elif collection_id == FILTERS:
            await self._ensure_filters()
        elif collection_id == HOTKEYS:
            await self._ensure_hotkeys()
        elif collection_id in (SCENE_COLLECTIONS, PROFILES) and self._names_stale:
            await self._refresh_names()

    async def _refresh_names(self) -> None:
        try:
            await self._read_names()
        except (Closed, Busy):
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, NOT_CONNECTED, retryable=True) from None
        except ProtocolFailure:
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, NOT_DONE) from None
        self._announce()

    def _control_state(self, object_id: str) -> State | None:
        kind = object_id.split(":", 1)[0]
        if kind == "output":
            output = self._outputs.get(object_id[len("output:"):])
            if output is None:
                return None
            values = {"active": output.active}
            if output.key == RECORD:
                values["paused"] = output.paused
            if output.key == STREAM:
                values["reconnecting"] = output.reconnecting
            return State(object_id, OUTPUTS, output.revision, values, available=output.available)
        if kind == "item":
            item = self._items.get(object_id)
            return None if item is None else State(object_id, ITEMS, item.revision, {"visible": item.enabled})
        if kind == "filter":
            item = self._filters.get(object_id)
            return None if item is None else State(object_id, FILTERS, item.revision, {"enabled": item.enabled})
        if kind == "media":
            item = self._inputs.get(object_id[len("media:"):])
            if item is None or item.kind not in MEDIA_KINDS or item.playing is None:
                return None
            return State(object_id, MEDIA, item.media_revision, {"playing": item.playing}, available=item.present)
        for prefix, store, collection in (("transition", self._transitions, TRANSITIONS),
                                          ("collection", self._scene_collections, SCENE_COLLECTIONS),
                                          ("profile", self._profiles, PROFILES)):
            if kind == prefix:
                named = store.get(object_id)
                return None if named is None else State(object_id, collection, named.revision, {"current": named.current})
        return None

    # ── outputs ──

    toggle_stream = _simple("toggle_stream", "ToggleStream", starts=STREAM)
    start_stream = _simple("start_stream", "StartStream", idempotent=(STATUS_OUTPUT_RUNNING,), starts=STREAM)
    stop_stream = _simple("stop_stream", "StopStream", idempotent=(STATUS_OUTPUT_NOT_RUNNING,))
    toggle_record = _simple("toggle_record", "ToggleRecord", starts=RECORD)
    start_record = _simple("start_record", "StartRecord", idempotent=(STATUS_OUTPUT_RUNNING,), starts=RECORD)
    stop_record = _simple("stop_record", "StopRecord", idempotent=(STATUS_OUTPUT_NOT_RUNNING,))
    toggle_record_pause = _simple("toggle_record_pause", "ToggleRecordPause", messages=RECORD_PAUSE_MESSAGES,
                                  needs_recording=True)
    pause_record = _simple("pause_record", "PauseRecord", idempotent=(STATUS_OUTPUT_PAUSED,),
                           messages=RECORD_PAUSE_MESSAGES, needs_recording=True)
    resume_record = _simple("resume_record", "ResumeRecord", idempotent=(STATUS_OUTPUT_NOT_PAUSED,),
                            messages=RECORD_PAUSE_MESSAGES, needs_recording=True)
    toggle_replay_buffer = _simple("toggle_replay_buffer", "ToggleReplayBuffer", messages=REPLAY_MESSAGES, starts=REPLAY)
    start_replay_buffer = _simple("start_replay_buffer", "StartReplayBuffer", idempotent=(STATUS_OUTPUT_RUNNING,),
                                  messages=REPLAY_MESSAGES, starts=REPLAY)
    stop_replay_buffer = _simple("stop_replay_buffer", "StopReplayBuffer", idempotent=(STATUS_OUTPUT_NOT_RUNNING,),
                                 messages=REPLAY_MESSAGES)
    save_replay = _simple("save_replay", "SaveReplayBuffer",
                          messages={**REPLAY_MESSAGES, STATUS_OUTPUT_NOT_RUNNING: REPLAY_OFF})
    toggle_virtual_camera = _simple("toggle_virtual_camera", "ToggleVirtualCam", messages=VIRTUALCAM_MESSAGES)
    start_virtual_camera = _simple("start_virtual_camera", "StartVirtualCam", idempotent=(STATUS_OUTPUT_RUNNING,),
                                   messages=VIRTUALCAM_MESSAGES)
    stop_virtual_camera = _simple("stop_virtual_camera", "StopVirtualCam", idempotent=(STATUS_OUTPUT_NOT_RUNNING,),
                                  messages=VIRTUALCAM_MESSAGES)
    cut_to_program = _simple("cut_to_program", "TriggerStudioModeTransition",
                             messages={STATUS_STUDIO_MODE_NOT_ACTIVE: NEEDS_STUDIO})

    @action("add_record_chapter")
    async def add_record_chapter(self, arguments: dict) -> dict:
        self._check_usable()
        if not self._outputs[RECORD].active:
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, NOT_RECORDING)
        await self._control("CreateRecordChapter", None, {STATUS_OUTPUT_NOT_RUNNING: NOT_RECORDING,
                                                          STATUS_INVALID_RESOURCE_STATE: NO_CHAPTERS})
        return {"accepted": True}

    @action("set_studio_mode")
    async def set_studio_mode(self, arguments: dict) -> dict:
        self._check_usable()
        await self._control("SetStudioModeEnabled", {"studioModeEnabled": arguments["enabled"]}, {})
        return {"accepted": True}

    @action("toggle_studio_mode")
    async def toggle_studio_mode(self, arguments: dict) -> dict:
        self._check_usable()
        target = not self._outputs[STUDIO].active
        await self._control("SetStudioModeEnabled", {"studioModeEnabled": target}, {})
        return {"accepted": True, "active": target}

    # ── scenes ──

    def _present_scene(self, object_id: str):
        scene = self._scenes.get(object_id[len("scene:"):]) if object_id.startswith("scene:") else None
        if scene is None or not scene.present:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_SCENE)
        return scene

    @action("set_preview_scene")
    async def set_preview_scene(self, arguments: dict) -> dict:
        self._check_usable()
        scene = self._present_scene(arguments["object_id"])
        await self._control("SetCurrentPreviewScene", {"sceneUuid": scene.uuid},
                            {STATUS_STUDIO_MODE_NOT_ACTIVE: NEEDS_STUDIO, STATUS_RESOURCE_NOT_FOUND: MISSING_SCENE})
        return {"object_id": arguments["object_id"]}

    async def _step_scene(self, delta: int) -> dict:
        self._check_usable()
        scenes = sorted((s for s in self._scenes.values() if s.present), key=lambda s: s.order)
        studio = self._outputs[STUDIO].active
        current = self._preview if studio else next((s.uuid for s in scenes if s.program), None)
        index = next((i for i, s in enumerate(scenes) if s.uuid == current), None)
        if not scenes or (index is not None and len(scenes) == 1):
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, NO_SCENES)
        target = scenes[0 if index is None else (index + delta) % len(scenes)]
        await self._control("SetCurrentPreviewScene" if studio else "SetCurrentProgramScene",
                            {"sceneUuid": target.uuid}, {STATUS_RESOURCE_NOT_FOUND: MISSING_SCENE,
                                                         STATUS_STUDIO_MODE_NOT_ACTIVE: NEEDS_STUDIO})
        return {"accepted": True}

    @action("next_scene")
    async def next_scene(self, arguments: dict) -> dict:
        return await self._step_scene(1)

    @action("previous_scene")
    async def previous_scene(self, arguments: dict) -> dict:
        return await self._step_scene(-1)

    @action("save_screenshot")
    async def save_screenshot(self, arguments: dict) -> dict:
        self._check_usable()
        scene = self._present_scene(arguments["object_id"])
        folder = arguments["folder"].rstrip("\\/")
        name = re.sub(r"[^\w -]+", "_", scene.name).strip() or "scene"
        path = f"{folder}\\{name}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        await self._control("SaveSourceScreenshot", {"sourceUuid": scene.uuid, "imageFormat": "png",
                                                     "imageFilePath": path},
                            {STATUS_RESOURCE_NOT_FOUND: MISSING_SCENE, STATUS_INVALID_RESOURCE_STATE: BAD_FOLDER})
        return {"object_id": arguments["object_id"]}

    # ── audio ──

    def _present_input(self, object_id: str):
        item = self._inputs.get(object_id[len("input:"):]) if object_id.startswith("input:") else None
        if item is None or not item.present:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_INPUT)
        return item

    @action("toggle_input_mute")
    async def toggle_input_mute(self, arguments: dict) -> dict:
        self._check_usable()
        item = self._present_input(arguments["object_id"])
        answer = await self._control("ToggleInputMute", {"inputUuid": item.uuid}, {STATUS_RESOURCE_NOT_FOUND: MISSING_INPUT})
        return {"object_id": arguments["object_id"], "muted": answer.get("inputMuted") is True}

    @action("set_input_volume")
    async def set_input_volume(self, arguments: dict) -> dict:
        self._check_usable()
        item = self._present_input(arguments["object_id"])
        await self._write_volume(item, arguments["volume_pct"])
        return {"object_id": arguments["object_id"]}

    @action("step_input_volume")
    async def step_input_volume(self, arguments: dict) -> dict:
        self._check_usable()
        item = self._present_input(arguments["object_id"])
        await self._ensure_volumes()
        if item.volume is None:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, VOLUME_UNKNOWN, retryable=True)
        await self._write_volume(item, mul_to_percent(item.volume) + arguments["step_pct"])
        return {"object_id": arguments["object_id"]}

    async def _write_volume(self, item, percent: float) -> None:
        await self._control("SetInputVolume", {"inputUuid": item.uuid, "inputVolumeMul": percent_to_mul(percent)},
                            {STATUS_RESOURCE_NOT_FOUND: MISSING_INPUT})

    # ── sources and filters ──

    @staticmethod
    def _item_target(object_id: str) -> tuple[str, int]:
        match = re.fullmatch(r"item:([0-9a-f-]{36}):([0-9]{1,10})", object_id)
        if match is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_ITEM)
        return match[1], int(match[2])

    async def _set_visible(self, object_id: str, visible: bool) -> None:
        scene_uuid, item_id = self._item_target(object_id)
        await self._control("SetSceneItemEnabled", {"sceneUuid": scene_uuid, "sceneItemId": item_id,
                                                    "sceneItemEnabled": visible},
                            {STATUS_RESOURCE_NOT_FOUND: MISSING_ITEM, STATUS_INVALID_RESOURCE_TYPE: MISSING_ITEM})

    @action("set_source_visible")
    async def set_source_visible(self, arguments: dict) -> dict:
        self._check_usable()
        await self._set_visible(arguments["object_id"], arguments["visible"])
        return {"object_id": arguments["object_id"]}

    @action("toggle_source_visible")
    async def toggle_source_visible(self, arguments: dict) -> dict:
        self._check_usable()
        object_id = arguments["object_id"]
        scene_uuid, item_id = self._item_target(object_id)
        known = self._items.get(object_id)
        if known is not None:
            visible = known.enabled
        else:                                  # a saved mapping can fire before the list is read
            try:
                data = await self._query("GetSceneItemEnabled", {"sceneUuid": scene_uuid, "sceneItemId": item_id})
            except RequestFailed:
                raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_ITEM) from None
            visible = data.get("sceneItemEnabled") is True
        await self._set_visible(object_id, not visible)
        return {"object_id": object_id}

    async def _present_filter(self, object_id: str) -> _Filter:
        await self._ensure_filters()
        item = self._filters.get(object_id)
        if item is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_FILTER)
        return item

    async def _set_filter(self, item: _Filter, enabled: bool) -> None:
        await self._control("SetSourceFilterEnabled", {"sourceUuid": item.source_uuid, "filterName": item.name,
                                                       "filterEnabled": enabled},
                            {STATUS_RESOURCE_NOT_FOUND: MISSING_FILTER})

    @action("set_filter_enabled")
    async def set_filter_enabled(self, arguments: dict) -> dict:
        self._check_usable()
        await self._set_filter(await self._present_filter(arguments["object_id"]), arguments["enabled"])
        return {"object_id": arguments["object_id"]}

    @action("toggle_filter")
    async def toggle_filter(self, arguments: dict) -> dict:
        self._check_usable()
        item = await self._present_filter(arguments["object_id"])
        await self._set_filter(item, not item.enabled)
        return {"object_id": arguments["object_id"]}

    # ── transitions, media, collections, hotkeys ──

    @action("set_transition")
    async def set_transition(self, arguments: dict) -> dict:
        self._check_usable()
        named = self._transitions.get(arguments["object_id"])
        if named is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_TRANSITION)
        await self._control("SetCurrentSceneTransition", {"transitionName": named.name},
                            {STATUS_RESOURCE_NOT_FOUND: MISSING_TRANSITION})
        return {"object_id": arguments["object_id"]}

    @action("set_transition_duration")
    async def set_transition_duration(self, arguments: dict) -> dict:
        self._check_usable()
        await self._control("SetCurrentSceneTransitionDuration", {"transitionDuration": arguments["duration_ms"]},
                            {STATUS_INVALID_RESOURCE_STATE: NOT_DONE})
        return {"accepted": True}

    def _present_media(self, object_id: str):
        item = self._inputs.get(object_id[len("media:"):]) if object_id.startswith("media:") else None
        if item is None or not item.present or item.kind not in MEDIA_KINDS:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_MEDIA)
        return item

    async def _media(self, item, verb: str) -> None:
        await self._control("TriggerMediaInputAction", {"inputUuid": item.uuid,
                                                        "mediaAction": _MEDIA_PREFIX + MEDIA_ACTIONS[verb]},
                            {STATUS_RESOURCE_NOT_FOUND: MISSING_MEDIA})

    @action("media_control")
    async def media_control(self, arguments: dict) -> dict:
        self._check_usable()
        await self._media(self._present_media(arguments["object_id"]), arguments["command"])
        return {"object_id": arguments["object_id"]}

    @action("toggle_media_playback")
    async def toggle_media_playback(self, arguments: dict) -> dict:
        self._check_usable()
        item = self._present_media(arguments["object_id"])
        try:
            status = await self._query("GetMediaInputStatus", {"inputUuid": item.uuid})
        except RequestFailed:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_MEDIA) from None
        playing = status.get("mediaState") == "OBS_MEDIA_STATE_PLAYING"
        await self._media(item, "pause" if playing else "play")
        return {"object_id": arguments["object_id"]}

    async def _switch_named(self, arguments: dict, store: dict, request: str, field: str) -> dict:
        self._check_usable()
        if self._names_stale:
            await self._refresh_names()
        named = store.get(arguments["object_id"])
        if named is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_NAMED)
        await self._control(request, {field: named.name}, {STATUS_RESOURCE_NOT_FOUND: MISSING_NAMED})
        return {"object_id": arguments["object_id"]}

    @action("switch_scene_collection")
    async def switch_scene_collection(self, arguments: dict) -> dict:
        return await self._switch_named(arguments, self._scene_collections, "SetCurrentSceneCollection", "sceneCollectionName")

    @action("switch_profile")
    async def switch_profile(self, arguments: dict) -> dict:
        return await self._switch_named(arguments, self._profiles, "SetCurrentProfile", "profileName")

    @action("trigger_hotkey")
    async def trigger_hotkey(self, arguments: dict) -> dict:
        self._check_usable()
        await self._ensure_hotkeys()
        named = self._hotkeys.get(arguments["object_id"])
        if named is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_NAMED)
        await self._control("TriggerHotkeyByName", {"hotkeyName": named.name}, {STATUS_RESOURCE_NOT_FOUND: MISSING_NAMED})
        return {"object_id": arguments["object_id"]}

    async def _preview_after_studio(self) -> None:
        """Turning studio mode on chooses a preview without announcing it."""
        try:
            await self._read_preview()
        except (Closed, Busy, ProtocolFailure):
            pass                               # the menu just shows no preview mark

    # ── toasts ──

    def _watch_start(self, key: str) -> None:
        """OBS answers success to a start it then abandons (a bad recording path,
        no stream service), and may only say so in its own window. Say so here."""
        if self._ready and not self._stopping:
            self._setup.background.start(lambda: self._check_started(key), name="obs-start-watch")

    async def _check_started(self, key: str) -> None:
        await asyncio.sleep(START_WATCH_S)
        if not self._stopping and not self._outputs[key].active:
            self._note_toast(OUTPUT_LABELS[key], DID_NOT_START, "error", topic=key)

    def _note_toast(self, title: str, status: str, tone: str, *, topic: str | None = None) -> None:
        """Queue a state toast. Called from the reader, so it never waits; the newest wins.

        `topic` is what a suppressed action could have caused. The first toast on
        it within QUIET_S of that action is the action's own, and isn't shown.
        """
        if self._quiet_until.pop(topic, 0) > time.monotonic():
            return
        if self._ready and not self._stopping:
            self._pending_toast = (title, status, tone)
            self._toast_wanted.set()

    async def _toast_loop(self) -> None:
        while True:
            await self._toast_wanted.wait()
            self._toast_wanted.clear()
            pending, self._pending_toast = self._pending_toast, None
            if pending is not None:
                await self._show_toast(*pending)

    async def _show_toast(self, title: str, status: str, tone: str, *, caption: str = "") -> None:
        children = [{"type": "text", "role": "title", "max_lines": 1, "text": title}]
        if caption:
            children.append({"type": "text", "role": "caption", "tone": "muted", "text": caption})
        pending = tone == "pending"
        children.append({"type": "row", "gap": 6, "children": [
            {"type": "status", "tone": tone, "pulse": pending},
            {"type": "text", "role": "caption", "tone": tone, "text": status}]})
        spec = {"v": 1, "toast_id": TOAST_ID, "size": "standard",
                "root": {"type": "column", "gap": 6, "children": children}}
        try:
            await self._setup.ui.toast(spec, permission_id=TOAST_PERMISSION)
        except Exception:
            pass                               # feedback must never break the control path

    # ── menus ──

    MENU_BASES = {"control": "hub", "scenes": "scenes", "mixer": "mixer", "sources": "sources"}
    HUB_PAGES = (("live", "Live", "Stream, recording, replay, camera", "waveform"),
                 ("scenes", "Scenes", "Switch what's on air", "layers"),
                 ("mixer", "Audio mixer", "Mute and volume", "volume"),
                 ("sources", "Sources", "Show and hide, filters", "filter"),
                 ("transitions", "Transitions", "Type and duration", "adjust"),
                 ("media", "Media", "Play and pause video sources", "play_pause"),
                 ("settings", "Settings", "Scene collection and profile", "settings"))
    PAGE_TITLES = {"hub": "OBS Studio", **{page: title for page, title, _s, _i in HUB_PAGES}}

    def _ref(self, prefix: str, object_id: str) -> str:
        key = digest(object_id)
        self._menu_refs[key] = object_id
        return f"{prefix}_{key}"

    def _deref(self, ident: str | None) -> str | None:
        return self._menu_refs.get(ident.split("_", 1)[-1]) if isinstance(ident, str) and "_" in ident else None

    def _sorted_scenes(self) -> list:
        return sorted((s for s in self._scenes.values() if s.present), key=lambda s: s.order)

    def _audio_inputs(self) -> list:
        return sorted((i for i in self._inputs.values() if i.present and i.muted is not None),
                      key=lambda i: i.name.casefold())

    async def _live_items(self) -> list:
        outputs = self._outputs
        stream, record, replay, camera, studio = (outputs[k] for k in (STREAM, RECORD, REPLAY, VIRTUALCAM, STUDIO))
        items = []
        stream_item = {"type": "action_item", "id": "stream", "icon": "waveform", "confirm": True,
                       "label": "Stop streaming" if stream.active else "Start streaming",
                       "disabled": not stream.available}
        record_item = {"type": "toggle_item", "id": "record", "icon": "record", "label": "Recording",
                       "on": record.active, "disabled": not record.available}
        if stream.active or record.active:
            await self._add_timecodes(stream_item, record_item, stream.active, record.active)
        if stream.reconnecting:
            stream_item["sublabel"] = "Reconnecting…"
        items += [stream_item, record_item]
        if record.active:
            items.append({"type": "toggle_item", "id": "pause", "icon": "play_pause", "label": "Pause recording",
                          "on": record.paused})
            items.append({"type": "action_item", "id": "chapter", "icon": "pin", "label": "Add chapter marker"})
        items.append({"type": "toggle_item", "id": "replay", "icon": "save", "label": "Replay buffer",
                      "on": replay.active, "disabled": not replay.available,
                      **({} if replay.available else {"sublabel": "Off in OBS settings"})})
        items.append({"type": "action_item", "id": "save_clip", "icon": "download", "label": "Save clip",
                      "disabled": not replay.active})
        items.append({"type": "toggle_item", "id": "camera", "icon": "camera", "label": "Virtual camera",
                      "on": camera.active, "disabled": not camera.available})
        items.append({"type": "toggle_item", "id": "studio", "icon": "window", "label": "Studio mode",
                      "on": studio.active, "disabled": not studio.available})
        if studio.active:
            items.append({"type": "action_item", "id": "cut", "icon": "arrow_right", "label": "Cut to program"})
        return items

    async def _add_timecodes(self, stream_item: dict, record_item: dict, streaming: bool, recording: bool) -> None:
        """Best effort: a page must still open if OBS doesn't answer this."""
        def clock(data: dict) -> str | None:
            value = data.get("outputTimecode")
            return value.split(".")[0] if isinstance(value, str) else None
        try:
            if streaming:
                data = await self._connection.request("GetStreamStatus")
                text = clock(data)
                skipped = data.get("outputSkippedFrames")
                if text:
                    stream_item["sublabel"] = (f"Live · {text} · {skipped} dropped" if type(skipped) is int and skipped
                                               else f"Live · {text}")
            if recording:
                text = clock(await self._connection.request("GetRecordStatus"))
                if text:
                    record_item["sublabel"] = text
        except (RequestFailed, Busy, Closed):
            pass

    async def _page(self, page: str, options: dict) -> tuple[list, list]:
        """(items, detail pages to declare) for one logical page."""
        if page == "hub":
            return ([{"type": "item", "id": "go_" + ident, "label": title, "sublabel": sub, "icon": icon, "opens": ident,
                      "chevron": True} for ident, title, sub, icon in self.HUB_PAGES], [])
        if page == "live":
            return await self._live_items(), []
        if page == "scenes":
            studio = self._outputs[STUDIO].active
            items = []
            for scene in self._sorted_scenes()[:MENU_ITEMS_MAX]:
                item = {"type": "action_item", "id": self._ref("sc", scene.object_id),
                        "icon": "confirm" if scene.program else "layers", "label": label_of(scene.name, "Scene")}
                if options.get("confirm_scene") == SCENE_TWICE and not scene.program:
                    item["confirm"] = True
                if scene.program:
                    item["sublabel"] = "On air"
                elif studio and scene.uuid == self._preview:
                    item["sublabel"] = "In preview"
                items.append(item)
            if studio:
                items.insert(0, {"type": "action_item", "id": "cut", "icon": "arrow_right", "label": "Cut to program"})
            return items, []
        if page == "mixer":
            await self._ensure_volumes()
            return self._mixer_items(options)
        if page == "sources":
            return await self._source_items(options)
        if page == "transitions":
            return self._transition_items(), []
        if page == "media":
            return self._media_items(), []
        if page == "settings":
            return await self._setting_items(), []
        if page.startswith("pi_"):
            await self._ensure_volumes()
            return self._input_items(self._deref(page)), []
        if page.startswith("ps_"):
            return await self._source_detail(self._deref(page)), []
        raise IntegrationError(ErrorCode.INVALID_ARGUMENT, BAD_MENU)

    def _mixer_items(self, options: dict) -> tuple[list, list]:
        items, pages = [], []
        direct = options.get("confirm_input") == INPUT_TOGGLE
        for index, item in enumerate(self._audio_inputs()[:MENU_ITEMS_MAX]):
            percent = None if item.volume is None else mul_to_percent(item.volume)
            icon = "volume_muted" if item.muted else "volume"
            sub = "Muted" if item.muted else "" if percent is None else f"{percent:.0f}%"
            ident = self._ref("in", item.object_id)
            row = {"id": ident, "label": label_of(item.name, "Input"), "icon": icon, "sublabel": sub}
            if index < DETAIL_PAGES_MAX and not direct:       # the host limits declared pages
                detail = self._ref("pi", item.object_id)
                row.update(type="item", opens=detail, chevron=True,
                           alternates={"double": {"id": "mute", "label": "Mute"}})
                pages.append({"type": "page", "id": detail, "title": row["label"], "lazy": True, "items": []})
            else:
                row.update(type="toggle_item", on=not item.muted)
            items.append(row)
        return items, pages

    def _input_items(self, object_id: str | None) -> list:
        item = self._present_input(object_id or "")
        controls = [{"type": "toggle_item", "id": "mute", "icon": "volume_muted", "label": "Muted", "on": bool(item.muted)}]
        if item.volume is not None:
            controls.append({"type": "slider_item", "id": "volume", "icon": "volume", "label": "Volume", "step": 2,
                             "fine_step": 0.5, "commit": "confirm",
                             "visual": {"type": "bar", "id": "level", "min": 0, "max": 100,
                                        "value": mul_to_percent(item.volume)}})
        return controls

    async def _source_items(self, options: dict) -> tuple[list, list]:
        await self._ensure_items()
        direct = options.get("confirm_source") == SOURCE_TOGGLE
        program = next((s for s in self._scenes.values() if s.present and s.program), None)
        if program is None:
            return [], []
        items, pages = [], []
        found = [i for i in self._items.values() if i.scene_uuid == program.uuid]
        for index, source in enumerate(found[:MENU_ITEMS_MAX]):
            ident = self._ref("so", source.object_id)
            row = {"id": ident, "label": label_of(source.name, "Source"), "icon": "layers",
                   "sublabel": "Visible" if source.enabled else "Hidden"}
            if index < DETAIL_PAGES_MAX and not direct:
                detail = self._ref("ps", source.object_id)
                row.update(type="item", opens=detail, chevron=True,
                           alternates={"double": {"id": "visible", "label": "Show or hide"}})
                pages.append({"type": "page", "id": detail, "title": row["label"], "lazy": True, "items": []})
            else:
                row.update(type="toggle_item", on=source.enabled)
            items.append(row)
        return items, pages

    async def _source_detail(self, object_id: str | None) -> list:
        await self._ensure_items()
        source = self._items.get(object_id or "")
        if source is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_ITEM)
        controls = [{"type": "toggle_item", "id": "visible", "icon": "window", "label": "Visible", "on": source.enabled}]
        await self._ensure_filters()
        found = [f for f in self._filters.values() if f.source_uuid == source.source_uuid]
        if found:
            controls.append({"type": "section", "label": "Filters"})
        for item in found[:MENU_ITEMS_MAX - 2]:
            controls.append({"type": "toggle_item", "id": self._ref("fi", item.object_id), "icon": "filter",
                             "label": label_of(item.name, "Filter"), "on": item.enabled})
        return controls

    def _transition_items(self) -> list:
        controls = []
        names = list(self._transitions.values())
        current = next((n for n in names if n.current), None)
        if names and current is not None:
            controls.append({"type": "choice_item", "id": "transition", "icon": "adjust", "label": "Transition",
                             "selected": "t_" + digest(current.object_id),
                             "options": [{"id": "t_" + digest(n.object_id), "label": label_of(n.name, "Transition")}
                                         for n in names]})
        if self._transition_duration is not None:
            controls.append({"type": "slider_item", "id": "duration", "icon": "hourglass", "label": "Duration (ms)",
                             "step": 50, "fine_step": 10, "commit": "confirm",
                             "visual": {"type": "bar", "id": "length", "min": 50, "max": 5000,
                                        "value": max(50, min(5000, self._transition_duration))}})
        return controls

    def _media_items(self) -> list:
        return [{"type": "action_item", "id": self._ref("me", "media:" + i.uuid), "icon": "play_pause",
                 "label": label_of(i.name, "Media"), "sublabel": "Playing" if i.playing else "",
                 "alternates": {"double": {"id": "restart", "label": "Restart"}}}
                for i in self._inputs.values() if i.present and i.kind in MEDIA_KINDS][:MENU_ITEMS_MAX]

    async def _setting_items(self) -> list:
        if self._names_stale:
            await self._refresh_names()
        controls = []
        for ident, label, store, prefix in (("collection", "Scene collection", self._scene_collections, "c_"),
                                            ("profile", "Profile", self._profiles, "p_")):
            names = list(store.values())
            current = next((n for n in names if n.current), None)
            if names and current is not None:
                controls.append({"type": "choice_item", "id": ident, "icon": "settings", "label": label,
                                 "selected": prefix + digest(current.object_id),
                                 "options": [{"id": prefix + digest(n.object_id), "label": label_of(n.name, label)}
                                             for n in names]})
        return controls

    def _ancestors(self, page: str, base: str) -> list:
        """Pages between the menu's root and `page`, nearest the root first.

        The host keeps only stack entries still declared in the returned spec,
        so each ancestor of an open page must be returned with it.
        """
        chain = []
        current = page
        while current != base:
            parent = ("mixer" if current.startswith("pi_") else "sources" if current.startswith("ps_")
                      else "hub" if current in self.PAGE_TITLES else None)
            if parent is None:
                raise IntegrationError(ErrorCode.INVALID_ARGUMENT, BAD_MENU)
            chain.append(current)
            current = parent
        return list(reversed(chain))

    async def menu_children(self, menu_id: str, page_id: str, cursor: str | None):
        base = self.MENU_BASES.get(menu_id)
        if base is None or cursor is not None:
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, BAD_MENU)
        self._check_usable()
        logical = base if page_id == "root" else page_id
        chain = self._ancestors(logical, base)
        declared: dict[str, dict] = {}
        if base == "hub":                      # every hub entry opens a page, loaded when entered
            for ident, title, _sub, _icon in self.HUB_PAGES:
                declared[ident] = {"type": "page", "id": ident, "title": title, "lazy": True, "items": []}
        options = self._menu_options.get(menu_id, {})
        items, details = await self._page(base, options)
        for detail in details:
            declared.setdefault(detail["id"], detail)
        title = self.PAGE_TITLES.get(base, "OBS Studio")
        root = {"type": "page", "id": "root", "title": title, "items": items, "empty_label": "Nothing to show"}
        for page in chain:
            items, details = await self._page(page, options)
            for detail in details:
                declared.setdefault(detail["id"], detail)
            old = declared.get(page, {})
            declared[page] = {"type": "page", "id": page, "title": old.get("title", self.PAGE_TITLES.get(page, "OBS Studio")),
                              "items": items, "empty_label": "Nothing to show"}
        return {"spec": {"v": 1, "surface": "menu", "size": "standard", "root": root,
                         "pages": list(declared.values())}, "next_cursor": None}

    async def _open_menu(self, menu_id: str, arguments: dict) -> dict:
        self._check_usable()
        # Only for this opening: the Controls window always gets the default rows.
        self._menu_options[menu_id] = {key: value for key, value in arguments.items() if key.startswith("confirm_")}
        try:
            opened = await self._setup.ui.open_menu(menu_id, permission_id=MENU_PERMISSION)
        except Exception:
            self._menu_options.pop(menu_id, None)
            raise
        if not opened:
            self._menu_options.pop(menu_id, None)
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, "The OBS menu could not be opened.")
        return {}

    @action("open_control_menu")
    async def open_control_menu(self, arguments: dict) -> dict:
        return await self._open_menu("control", arguments)

    @action("open_scenes_menu")
    async def open_scenes_menu(self, arguments: dict) -> dict:
        return await self._open_menu("scenes", arguments)

    @action("open_mixer_menu")
    async def open_mixer_menu(self, arguments: dict) -> dict:
        return await self._open_menu("mixer", arguments)

    @action("open_sources_menu")
    async def open_sources_menu(self, arguments: dict) -> dict:
        return await self._open_menu("sources", arguments)

    async def _menu_write(self, title: str, label: str, coro) -> None:
        self._toast_sequence += 1
        sequence = self._toast_sequence
        await self._show_toast(title, "Sending…", "pending", caption=label)
        try:
            await coro
        except Exception as error:
            if sequence == self._toast_sequence:
                unknown = getattr(getattr(error, "info", error), "outcome_unknown", False)
                await self._show_toast(title, "Outcome unknown" if unknown else getattr(error, "message", "Failed"),
                                       "unknown" if unknown else "error", caption=label)
            raise
        if sequence == self._toast_sequence:
            await self._show_toast(title, "Done", "success", caption=label)

    async def on_menu_event(self, menu_id: str, event: dict) -> None:
        kind, item, page, value = event.get("kind"), event.get("item"), event.get("page"), event.get("value")
        if kind == "closed":
            self._menu_options.pop(menu_id, None)
        elif kind == "value_changed":        # a reverted edit carries the original value
            await self._menu_value(page, item, value)
        elif kind == "alternate":
            await self._menu_alternate(item, event.get("alternate"))
        elif kind == "select":
            await self._menu_select(page, item, value, self._menu_options.get(menu_id, {}))

    async def _menu_value(self, page, item, value) -> None:
        """Live slider movement: no toast, the newest value simply wins."""
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return
        if item == "volume":
            source = self._present_input(self._deref(page) or "")
            await self._write_volume(source, value)
        elif item == "duration":
            await self.set_transition_duration({"duration_ms": int(value)})

    async def _menu_alternate(self, item, alternate) -> None:
        target = self._deref(item)
        if alternate == "mute" and target is not None:
            source = self._present_input(target)
            await self._menu_write(label_of(source.name, "Input"), "Toggle mute",
                                   self.toggle_input_mute({"object_id": target}))
        elif alternate == "visible" and target is not None:
            await self._menu_write(self._item_label(target), "Show or hide",
                                   self.toggle_source_visible({"object_id": target}))
        elif alternate == "restart" and target is not None:
            await self._menu_write(label_of(self._present_media(target).name, "Media"), "Restart",
                                   self.media_control({"object_id": target, "command": "restart"}))

    def _item_label(self, object_id: str) -> str:
        item = self._items.get(object_id)
        return label_of(item.name, "Source") if item is not None else "Source"

    async def _menu_select(self, page, item, value, options: dict) -> None:
        if not isinstance(item, str):
            return
        live = {"stream": ("Streaming", "Toggle", self.toggle_stream), "record": ("Recording", "Toggle", self.toggle_record),
                "pause": ("Recording", "Pause or resume", self.toggle_record_pause),
                "chapter": ("Recording", "Add chapter", self.add_record_chapter),
                "replay": ("Replay buffer", "Toggle", self.toggle_replay_buffer),
                "save_clip": ("Replay buffer", "Save clip", self.save_replay),
                "camera": ("Virtual camera", "Toggle", self.toggle_virtual_camera),
                "studio": ("Studio mode", "Toggle", self.toggle_studio_mode),
                "cut": ("Studio mode", "Cut to program", self.cut_to_program)}
        if item in live:
            title, label, handler = live[item]
            await self._menu_write(title, label, handler({}))
            return
        if item == "mute" or item == "volume":
            target = self._deref(page)
            source = self._present_input(target or "")
            name = label_of(source.name, "Input")
            if item == "mute":
                on = value if isinstance(value, bool) else not source.muted
                await self._menu_write(name, "Mute" if on else "Unmute",
                                       self.set_input_mute({"object_id": target, "muted": on}))
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                await self._menu_write(name, f"Volume: {value:.0f}%", self._write_volume(source, value))
            return
        if item == "visible":
            target = self._deref(page)
            on = value if isinstance(value, bool) else True
            await self._menu_write(self._item_label(target or ""), "Show" if on else "Hide",
                                   self.set_source_visible({"object_id": target, "visible": on}))
            return
        if item == "transition" or item == "collection" or item == "profile":
            store, handler, label = {"transition": (self._transitions, self.set_transition, "Transition"),
                                     "collection": (self._scene_collections, self.switch_scene_collection, "Scene collection"),
                                     "profile": (self._profiles, self.switch_profile, "Profile")}[item]
            named = next((n for n in store.values() if isinstance(value, str)
                          and value.split("_", 1)[-1] == digest(n.object_id)), None)
            if named is not None:
                await self._menu_write(label, label_of(named.name, label), handler({"object_id": named.object_id}))
            return
        if item == "duration":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                await self._menu_write("Transition", f"Duration: {int(value)} ms",
                                       self.set_transition_duration({"duration_ms": int(value)}))
            return
        target = self._deref(item)
        if target is None:
            return
        if item.startswith("sc_"):
            scene = self._present_scene(target)
            studio = self._outputs[STUDIO].active and options.get("confirm_scene") not in (SCENE_ON_AIR, SCENE_TWICE)
            await self._menu_write(label_of(scene.name, "Scene"), "Preview" if studio else "On air",
                                   (self.set_preview_scene if studio else self.switch_scene)({"object_id": target}))
        elif item.startswith("in_"):          # rows past the detail-page limit toggle directly
            source = self._present_input(target)
            on = value if isinstance(value, bool) else bool(source.muted)
            await self._menu_write(label_of(source.name, "Input"), "Unmute" if on else "Mute",
                                   self.set_input_mute({"object_id": target, "muted": not on}))
        elif item.startswith("so_"):
            on = value if isinstance(value, bool) else True
            await self._menu_write(self._item_label(target), "Show" if on else "Hide",
                                   self.set_source_visible({"object_id": target, "visible": on}))
        elif item.startswith("fi_"):
            filter_item = await self._present_filter(target)
            on = value if isinstance(value, bool) else not filter_item.enabled
            await self._menu_write(label_of(filter_item.name, "Filter"), "Enable" if on else "Disable",
                                   self._set_filter(filter_item, on))
        elif item.startswith("me_"):
            await self._menu_write(label_of(self._present_media(target).name, "Media"), "Play or pause",
                                   self.toggle_media_playback({"object_id": target}))
