"""Google Home Lights provider using MIDImap's host-owned web session.

This development package implements discovery, real state reads and guarded
UpdateTraits writes. Write responses must report the requested target state;
HTTP 200 alone is never accepted as a light command acknowledgement. Physical
results still require a separate observed state check.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any
from dataclasses import replace

from midimap_integration_sdk import (
    ErrorCode, HostCallError, IntegrationError, ObjectInfo, Page, Provider, Setup,
    State, action,
)

import google_protocol as wire

SESSION = "google_home"
LIGHTS = "lights"
AUTOMATIONS = "automations"
ROOMS = "rooms"
THERMOSTATS = "thermostats"
PAGE_SIZE = 100
CATALOG_INTERVAL_S = 60
MAX_LIGHTS = 4000
_CURSOR = re.compile(r"([0-9]{1,9})\.([0-9]{1,6})\Z")

NOT_CONNECTED = "Connect Google Home and choose a home in integration setup."
HOME_MISSING = "The selected Google Home is no longer available; choose a home again."
BAD_RESPONSE = "Google Home returned a response this integration cannot read."
_PROTOCOL_REASON_CODES = {
    "a GetTraits response must hold device records": "records",
    "a trait list must be an array": "trait-list",
    "a trait must have a name": "trait-name",
    "trait attributes must be an array": "attribute-list",
    "a trait attribute is malformed": "attribute-shape",
    "a trait attribute is repeated": "attribute-duplicate",
    "a trait must be a [name, parameters] pair": "trait-shape",
    "a trait's parameters must be an array": "parameter-list",
    "a parameter must be a [name, value] pair": "parameter-shape",
    "a typed value must be an array": "value-shape",
    "a typed value has an unknown populated field": "value-field",
    "a typed value must have exactly one populated field": "value-variant",
    "an UpdateTraits acknowledgement has unexpected records": "write-records",
    "an UpdateTraits acknowledgement has unexpected fields": "write-fields",
    "an UpdateTraits acknowledgement targets another device": "write-target",
    "an UpdateTraits acknowledgement reports an error": "write-error",
    "an UpdateTraits acknowledgement reports an offline device": "write-offline",
    "an UpdateTraits acknowledgement does not match the request": "write-mismatch",
    "an UpdateTraits acknowledgement has unknown access metadata": "write-metadata",
}
GOOGLE_UNAVAILABLE = "Google Home did not answer; try again."
GOOGLE_AUTH = "The Google Home sign-in needs attention; reconnect it in setup."
GOOGLE_BUSY = "Google Home is temporarily limiting requests; try again later."
CATALOG_CHANGED = "The light list changed during discovery; MIDImap will read it again."
TOO_MANY = "This Google Home has more lights than this integration supports."
TOGGLE_STATE_UNKNOWN = "Google Home has not reported an online on/off state for this light."
TOGGLE_PENDING = "The previous light command has not appeared in Google Home state yet."
MISSING_LIGHT = "The selected light is no longer in this Google Home."
UNSUPPORTED_ACTION = "The selected light does not support this control."
ACCESS_UNVERIFIED = "Google Home did not provide a recognizable control-access result for this light."


def _label(name: str) -> str:
    text = " ".join("".join(" " if ord(c) < 32 or 127 <= ord(c) < 160 else c
                            for c in name).split())
    return text[:128] or "Light"


def _protocol_error(stage: str, error: wire.ProtocolError,
                    *, outcome_unknown: bool = False) -> IntegrationError:
    # Only authored tokens leave the worker. No exception or Google response
    # text, device IDs, names, cookie values or paths enter the message.
    reason = _PROTOCOL_REASON_CODES.get(str(error), "unexpected-shape")
    return IntegrationError(ErrorCode.PROVIDER_ERROR,
                            f"{BAD_RESPONSE} ({stage}/{reason})",
                            outcome_unknown=outcome_unknown)


class GoogleHomeProvider(Provider):
    def __init__(self):
        self._setup: Setup | None = None
        self._structure_id: str | None = None
        self._session_epoch: int | None = None
        self._lights: dict[str, wire.DeviceEntry] = {}
        self._rooms: dict[str, wire.RoomEntry] = {}
        self._devices: dict[str, wire.DeviceEntry] = {}
        self._thermostats: dict[str, wire.DeviceEntry] = {}
        self._thermostat_caps: dict[str, tuple[str, ...]] = {}
        self._menu_room: str | None = None
        self._menu_targets: dict[str, str] = {}
        self._menu_offset = 0
        self._toast_sequence = 0
        self._automations: dict[str, wire.AutomationEntry] = {}
        self._automation_revision = 0
        self._capabilities: dict[str, tuple[str, ...]] = {}
        self._unverified_access: set[str] = set()
        self._catalog_revision = 0
        self._state: dict[str, State] = {}
        self._pending_power: dict[str, bool] = {}
        self._pending_thermostat_mode: dict[str, str] = {}
        self._last_thermostat_mode: dict[str, str] = {}
        self._wire_lock = asyncio.Lock()
        self._catalog_lock = asyncio.Lock()
        self._refresh_event = asyncio.Event()
        self._refresh_ids: set[str] = set()
        self._stopping = False

    async def initialize(self, setup: Setup) -> None:
        self._setup = setup
        info = await setup.host.web_session_info(SESSION)
        if not info.connected or not info.structure_id:
            raise IntegrationError(ErrorCode.NEEDS_SETUP, NOT_CONNECTED)
        self._structure_id, self._session_epoch = info.structure_id, info.epoch
        await self._refresh_catalog()
        setup.background.start(self._catalog_loop, name="google-home-catalog")
        setup.background.start(self._write_refresh_loop, name="google-home-state-refresh")

    async def shutdown(self) -> None:
        self._stopping = True
        self._refresh_event.set()

    async def _call(self, operation: str, body: Any, *, write: bool = False) -> Any:
        if self._setup is None or self._stopping:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, GOOGLE_UNAVAILABLE)
        try:
            async with self._wire_lock:
                answer = await self._setup.host.web_session_request(SESSION, operation, body)
        except HostCallError:
            raise
        if answer.status in (401, 403):
            raise IntegrationError(ErrorCode.NEEDS_SETUP, GOOGLE_AUTH,
                                   outcome_unknown=write)
        if answer.status == 429:
            raise IntegrationError(ErrorCode.SATURATED, GOOGLE_BUSY,
                                   retryable=True, outcome_unknown=write)
        if answer.status >= 500:
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, GOOGLE_UNAVAILABLE,
                                   retryable=True, outcome_unknown=write)
        if answer.status != 200:
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, BAD_RESPONSE,
                                   outcome_unknown=write)
        return answer.data

    async def _refresh_catalog(self) -> None:
        async with self._catalog_lock:
            if self._setup is None or self._structure_id is None:
                raise IntegrationError(ErrorCode.NEEDS_SETUP, NOT_CONNECTED)
            info = await self._setup.host.web_session_info(SESSION)
            if not info.connected or info.structure_id != self._structure_id \
                    or info.epoch != self._session_epoch:
                raise IntegrationError(ErrorCode.NEEDS_SETUP, HOME_MISSING)
            stage = "home graph"
            try:
                raw_graph = await self._call("get_home_graph", wire.get_home_graph_request())
                graph = wire.parse_home_graph(raw_graph)
                devices = {wire.stable_id("device", self._structure_id, entry.google_device_id): entry
                           for entry in graph if entry.structure_id == self._structure_id
                           and not entry.is_group and not entry.is_routine}
                rooms = {wire.stable_id("room", self._structure_id, room.room_id): room
                         for room in wire.parse_home_rooms(raw_graph)
                         if room.structure_id == self._structure_id}
                selected = wire.lights(graph, self._structure_id)
                thermostats = {wire.stable_id("thermostat", self._structure_id, entry.google_device_id): entry
                               for entry in wire.thermostats(graph, self._structure_id)}
                if len(selected) > MAX_LIGHTS:
                    raise IntegrationError(ErrorCode.SATURATED, TOO_MANY)
                found: dict[str, wire.DeviceEntry] = {}
                for entry in selected:
                    key = wire.object_id(self._structure_id, entry.google_device_id)
                    if key in found:
                        raise wire.ProtocolError("the home graph repeated a light")
                    found[key] = entry
                access: dict[str, dict | None] = {}
                ids = list(dict.fromkeys([entry.google_device_id for entry in selected]
                                         + [entry.google_device_id for entry in thermostats.values()]))
                for offset in range(0, len(ids), wire.GET_TRAITS_BATCH_MAX):
                    stage = "trait access"
                    response = await self._call(
                        "get_traits", wire.get_traits_request(
                            ids[offset:offset + wire.GET_TRAITS_BATCH_MAX]))
                    access.update(wire.parse_trait_access(response))
                caps = {key: wire.light_capabilities(
                    entry.trait_names, access[entry.google_device_id])
                    if access.get(entry.google_device_id) is not None else ()
                    for key, entry in found.items()}
                unverified = {key for key, entry in found.items()
                              if access.get(entry.google_device_id) is None}
                thermostats = {key: replace(entry, attributes={
                    **entry.attributes,
                    **(access.get(entry.google_device_id) or {}).get(wire.TRAIT_TEMPERATURE_SETTING, {})})
                    for key, entry in thermostats.items()}
                thermostat_caps = {key: wire.thermostat_capabilities(entry, access[entry.google_device_id])
                                   if access.get(entry.google_device_id) is not None else ()
                                   for key, entry in thermostats.items()}
            except wire.ProtocolError as error:
                raise _protocol_error(stage, error) from None
            rooms_changed = rooms != self._rooms
            thermostat_changed = thermostats != self._thermostats or thermostat_caps != self._thermostat_caps
            self._devices = devices
            changed = (found != self._lights or caps != self._capabilities
                       or unverified != self._unverified_access)
            if changed:
                self._lights, self._capabilities = found, caps
                self._unverified_access = unverified
                self._catalog_revision += 1
                self._state = {key: state for key, state in self._state.items()
                               if key in found or key in rooms or key in thermostats}
                self._pending_power = {key: on for key, on in self._pending_power.items()
                                       if key in found or key in thermostats}
                self._setup.events.publish_objects_changed(LIGHTS)
            if rooms_changed:
                self._rooms = rooms
                self._catalog_revision += 1
                self._setup.events.publish_objects_changed(ROOMS)
            if thermostat_changed:
                self._thermostats, self._thermostat_caps = thermostats, thermostat_caps
                self._catalog_revision += 1
                self._setup.events.publish_objects_changed(THERMOSTATS)

    async def _catalog_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(CATALOG_INTERVAL_S)
            if self._stopping:
                return
            try:
                await self._refresh_catalog()
            except IntegrationError as error:
                if error.code in (ErrorCode.NEEDS_SETUP, ErrorCode.PROVIDER_ERROR):
                    self._setup.events.report_unavailable(error.code, error.message)
                    return
                # Keep the previous catalog on a transient refresh failure.

    async def _write_refresh_loop(self) -> None:
        """Coalesce post-write state reads to at most one per second."""
        while not self._stopping:
            await self._refresh_event.wait()
            self._refresh_event.clear()
            if self._stopping:
                return
            await asyncio.sleep(1)
            ids = tuple(self._refresh_ids)
            self._refresh_ids.clear()
            try:
                for state in await self.read_states(ids):
                    self._setup.events.publish_state(state)
            except (IntegrationError, ValueError):
                # A later explicit state read or write will retry. The action's
                # accepted result is not turned into a failure by this read.
                pass

    def _schedule_write_refresh(self, object_id: str) -> None:
        self._refresh_ids.add(object_id)
        self._refresh_event.set()

    async def list_objects(self, collection_id: str, cursor: str | None) -> Page:
        if collection_id == THERMOSTATS:
            objects = [ObjectInfo(key, THERMOSTATS, _label(entry.display_name), None,
                                  self._thermostat_caps[key])
                       for key, entry in sorted(self._thermostats.items(),
                                                key=lambda pair: (pair[1].display_name, pair[0]))]
            start = 0
            if cursor is not None:
                match = _CURSOR.fullmatch(cursor)
                if match is None or int(match[1]) != self._catalog_revision or int(match[2]) > len(objects):
                    raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, CATALOG_CHANGED, retryable=True)
                start = int(match[2])
            end = start + PAGE_SIZE
            return Page(objects[start:end], f"{self._catalog_revision}.{end}" if end < len(objects) else None)
        if collection_id == ROOMS:
            objects = [ObjectInfo(key, ROOMS, _label(room.display_name), None,
                                  self._room_capabilities(key))
                       for key, room in sorted(self._rooms.items(),
                                               key=lambda pair: (pair[1].display_name, pair[0]))]
            start = 0
            if cursor is not None:
                match = _CURSOR.fullmatch(cursor)
                if match is None or int(match[1]) != self._catalog_revision or int(match[2]) > len(objects):
                    raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, CATALOG_CHANGED, retryable=True)
                start = int(match[2])
            end = start + PAGE_SIZE
            return Page(objects[start:end], f"{self._catalog_revision}.{end}" if end < len(objects) else None)
        if collection_id == AUTOMATIONS:
            await self._refresh_automations()
            # Capability identifies the action/collection, not current enabled
            # state. An undeclared "disabled" action is an invalid SDK result.
            # Execution and the automation menu separately enforce enabled.
            objects = [ObjectInfo(key, AUTOMATIONS, _label(entry.display_name), None,
                                  ("run_automation",))
                       for key, entry in sorted(self._automations.items(),
                                                key=lambda pair: (pair[1].display_name, pair[0]))]
            start = 0
            if cursor is not None:
                match = _CURSOR.fullmatch(cursor)
                if match is None or int(match[1]) != self._automation_revision \
                        or int(match[2]) > len(objects):
                    raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, CATALOG_CHANGED,
                                           retryable=True)
                start = int(match[2])
            end = start + PAGE_SIZE
            return Page(objects[start:end],
                        f"{self._automation_revision}.{end}" if end < len(objects) else None)
        if collection_id != LIGHTS:
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "Unknown light collection.")
        objects = [ObjectInfo(key, LIGHTS, _label(entry.display_name), None,
                              self._capabilities[key])
                   for key, entry in sorted(self._lights.items(),
                                            key=lambda pair: (pair[1].display_name, pair[0]))]
        start = 0
        if cursor is not None:
            match = _CURSOR.fullmatch(cursor)
            if match is None or int(match[1]) != self._catalog_revision \
                    or int(match[2]) > len(objects):
                raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, CATALOG_CHANGED,
                                       retryable=True)
            start = int(match[2])
        end = start + PAGE_SIZE
        next_cursor = f"{self._catalog_revision}.{end}" if end < len(objects) else None
        return Page(objects[start:end], next_cursor)

    async def read_states(self, object_ids: Sequence[str]) -> list[State]:
        automation_ids = [key for key in dict.fromkeys(object_ids) if key in self._automations]
        if automation_ids:
            await self._refresh_automations()
        requested = [(key, self._lights[key]) for key in dict.fromkeys(object_ids)
                     if key in self._lights]
        states: list[State] = [State(key, AUTOMATIONS, self._automation_revision,
                                     {"enabled": self._automations[key].enabled}, True)
                               for key in automation_ids if key in self._automations]
        thermostat_requests = [(key, self._thermostats[key]) for key in dict.fromkeys(object_ids)
                               if key in self._thermostats]
        try:
            for offset in range(0, len(thermostat_requests), wire.GET_TRAITS_BATCH_MAX):
                batch = thermostat_requests[offset:offset + wire.GET_TRAITS_BATCH_MAX]
                observed = wire.parse_get_traits(await self._call("get_traits", wire.get_traits_request(
                    [entry.google_device_id for _, entry in batch])))
                for key, entry in batch:
                    current = observed.get(entry.google_device_id)
                    if current is None:
                        continue
                    values = {name: value for name, value in current.values.items()
                              if name in ("on", "mode", "temperature_unit", "target_temperature_c", "ambient_temperature_c")}
                    previous = self._state.get(key)
                    revision = (previous.revision if previous is not None and previous.available == current.available
                                and dict(previous.values) == values else 1 if previous is None else previous.revision + 1)
                    state = State(key, THERMOSTATS, revision, values, current.available)
                    if current.available and values.get("on") == self._pending_power.get(key):
                        self._pending_power.pop(key, None)
                    mode = values.get("mode")
                    if current.available and isinstance(mode, str):
                        if mode != "off":
                            self._last_thermostat_mode[key] = mode
                        if mode == self._pending_thermostat_mode.get(key):
                            self._pending_thermostat_mode.pop(key, None)
                    self._state[key] = state
                    states.append(state)
        except wire.ProtocolError as error:
            raise _protocol_error("thermostat-state", error) from None
        for key in dict.fromkeys(object_ids):
            if key not in self._rooms:
                continue
            members = self._room_light_ids(key)
            observed = await self.read_states(members)
            available = bool(members) and len(observed) == len(members) and all(
                state.available and isinstance(state.values.get("on"), bool) for state in observed)
            values = {"on": any(state.values["on"] for state in observed)} if available else {}
            previous = self._state.get(key)
            revision = (previous.revision if previous is not None and previous.available == available
                        and dict(previous.values) == values else 1 if previous is None else previous.revision + 1)
            room_state = State(key, ROOMS, revision, values, available)
            self._state[key] = room_state
            states.append(room_state)
        try:
            for offset in range(0, len(requested), wire.GET_TRAITS_BATCH_MAX):
                batch = requested[offset:offset + wire.GET_TRAITS_BATCH_MAX]
                result = await self._call(
                    "get_traits", wire.get_traits_request(
                        [entry.google_device_id for _key, entry in batch]))
                by_google_id = wire.parse_get_traits(result)
                for key, entry in batch:
                    observed = by_google_id.get(entry.google_device_id)
                    if observed is None:
                        continue
                    previous = self._state.get(key)
                    values = {name: value for name, value in observed.values.items()
                              if name in ("on", "brightness_pct", "color_temperature_k",
                                          "hue_degrees", "saturation_pct", "color_value_pct")}
                    revision = (previous.revision if previous is not None
                                and previous.available == observed.available
                                and dict(previous.values) == values
                                else 1 if previous is None else previous.revision + 1)
                    state = State(key, LIGHTS, revision, values, observed.available)
                    self._state[key] = state
                    if state.available and state.values.get("on") is self._pending_power.get(key):
                        self._pending_power.pop(key, None)
                    states.append(state)
        except wire.ProtocolError as error:
            raise _protocol_error("state", error) from None
        return states

    def _room_light_ids(self, room_id: str) -> list[str]:
        room = self._rooms.get(room_id)
        if room is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, "The selected room is unavailable.")
        return [key for key, entry in self._lights.items() if entry.google_device_id in room.device_ids]

    def _room_capabilities(self, room_id: str) -> tuple[str, ...]:
        members = self._room_light_ids(room_id)
        if not members:
            return ("browse_room",)
        caps = ["browse_room"]
        if all("set_power" in self._capabilities.get(key, ()) for key in members):
            caps.extend(("set_room_power", "toggle_room_power"))
        if all("set_brightness" in self._capabilities.get(key, ()) for key in members):
            caps.append("set_room_brightness")
        return tuple(caps)

    @action("set_room_power")
    async def set_room_power(self, arguments: dict) -> dict:
        room_id = arguments["object_id"]
        members = self._room_light_ids(room_id)
        if not members:
            raise IntegrationError(ErrorCode.TARGET_MISSING, "This room has no controllable lights.")
        entries = [self._check_target(key, "set_power") for key in members]
        traits = wire.power_traits(arguments["on"])
        response = await self._call("update_traits", wire.update_traits_request(
            [wire.device_update(entry.device_ref, traits) for entry in entries]), write=True)
        try:
            wire.verify_update_acks(response, {entry.google_device_id: traits for entry in entries})
        except wire.ProtocolError as error:
            raise _protocol_error("room-write-ack", error, outcome_unknown=True) from None
        for key in members:
            self._pending_power[key] = arguments["on"]
            self._schedule_write_refresh(key)
        return {"object_id": room_id, "accepted": True}

    @action("set_room_brightness")
    async def set_room_brightness(self, arguments: dict) -> dict:
        room_id = arguments["object_id"]
        members = self._room_light_ids(room_id)
        if not members:
            raise IntegrationError(ErrorCode.TARGET_MISSING, "This room has no controllable lights.")
        entries = [self._check_target(key, "set_brightness") for key in members]
        traits = wire.set_level_traits(arguments["brightness_pct"])
        response = await self._call("update_traits", wire.update_traits_request(
            [wire.device_update(entry.device_ref, traits) for entry in entries]), write=True)
        try:
            wire.verify_update_acks(response, {entry.google_device_id: traits for entry in entries})
        except wire.ProtocolError as error:
            raise _protocol_error("room-write-ack", error, outcome_unknown=True) from None
        for key in members:
            self._pending_power[key] = arguments["brightness_pct"] > 0
            self._schedule_write_refresh(key)
        return {"object_id": room_id, "accepted": True}

    @action("toggle_room_power")
    async def toggle_room_power(self, arguments: dict) -> dict:
        room_id = arguments["object_id"]
        states = await self.read_states([room_id])
        if not states or not states[0].available or not isinstance(states[0].values.get("on"), bool):
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, "The room's light state is unavailable.")
        if any(key in self._pending_power for key in self._room_light_ids(room_id)):
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, TOGGLE_PENDING)
        return await self.set_room_power({"object_id": room_id, "on": not states[0].values["on"]})

    def _thermostat_target(self, key: str, action_id: str) -> wire.DeviceEntry:
        entry = self._thermostats.get(key)
        if entry is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, "The selected thermostat is unavailable.")
        if action_id not in self._thermostat_caps.get(key, ()):
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "This thermostat does not support that control.")
        return entry

    @action("set_thermostat_temperature")
    async def set_thermostat_temperature(self, arguments: dict) -> dict:
        key = arguments["object_id"]
        entry = self._thermostat_target(key, "set_thermostat_temperature")
        low, high = wire.thermostat_range(entry)
        if not low <= arguments["temperature_c"] <= high:
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "The temperature is outside this device's range.")
        await self._send_update(entry, wire.thermostat_temperature_traits(arguments["temperature_c"]))
        self._schedule_write_refresh(key)
        return {"object_id": key, "accepted": True}

    @action("set_thermostat_mode")
    async def set_thermostat_mode(self, arguments: dict) -> dict:
        key = arguments["object_id"]
        entry = self._thermostat_target(key, "set_thermostat_mode")
        if arguments["mode"] not in wire.thermostat_modes(entry):
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "This mode is not offered by the thermostat.")
        await self._send_update(entry, wire.thermostat_mode_traits(arguments["mode"]))
        self._pending_thermostat_mode[key] = arguments["mode"]
        self._schedule_write_refresh(key)
        return {"object_id": key, "accepted": True}

    @action("set_thermostat_power")
    async def set_thermostat_power(self, arguments: dict) -> dict:
        key = arguments["object_id"]
        entry = self._thermostat_target(key, "set_thermostat_power")
        await self._send_update(entry, wire.power_traits(arguments["on"]))
        self._pending_power[key] = arguments["on"]
        self._schedule_write_refresh(key)
        return {"object_id": key, "accepted": True}

    @action("toggle_thermostat")
    async def toggle_thermostat(self, arguments: dict) -> dict:
        key = arguments["object_id"]
        entry = self._thermostat_target(key, "toggle_thermostat")
        states = await self.read_states([key])
        if "set_thermostat_power" in self._thermostat_caps.get(key, ()):
            if key in self._pending_power:
                raise IntegrationError(ErrorCode.PROVIDER_ERROR, "The previous power command has not appeared yet.")
            if not states or not states[0].available or not isinstance(states[0].values.get("on"), bool):
                raise IntegrationError(ErrorCode.PROVIDER_ERROR, "The thermostat power state is unavailable.")
            return await self.set_thermostat_power({"object_id": key, "on": not states[0].values["on"]})
        if key in self._pending_thermostat_mode:
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, "The previous thermostat mode has not appeared yet.")
        if not states or not states[0].available or not isinstance(states[0].values.get("mode"), str):
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, "The thermostat mode is unavailable.")
        mode = states[0].values["mode"]
        available = wire.thermostat_modes(entry)
        if mode == "off":
            target = self._last_thermostat_mode.get(key)
            if target not in available or target == "off":
                target = next(candidate for candidate in available if isinstance(candidate, str)
                              and candidate and candidate != "off")
        else:
            target = "off"
        return await self.set_thermostat_mode({"object_id": key, "mode": target})

    async def _refresh_automations(self) -> None:
        if self._structure_id is None:
            raise IntegrationError(ErrorCode.NEEDS_SETUP, NOT_CONNECTED)
        try:
            entries = wire.parse_automations(await self._call(
                "list_automations", wire.list_automations_request(self._structure_id)),
                self._structure_id)
            found = {wire.stable_id("automation", self._structure_id, entry.automation_id): entry
                     for entry in entries}
        except wire.ProtocolError as error:
            raise _protocol_error("automations", error) from None
        if found != self._automations:
            self._automations = found
            self._automation_revision += 1
            self._setup.events.publish_objects_changed(AUTOMATIONS)

    @action("run_automation")
    async def run_automation(self, arguments: dict) -> dict:
        object_id = arguments["object_id"]
        await self._refresh_automations()
        entry = self._automations.get(object_id)
        if entry is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, "The selected automation is unavailable.")
        if not entry.enabled:
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "The selected automation is disabled.")
        response = await self._call("execute_automation", wire.execute_automation_request(
            entry.structure_id, entry.automation_id, entry.backend), write=True)
        try:
            wire.verify_automation_ack(response)
        except wire.ProtocolError as error:
            raise _protocol_error("automation-ack", error, outcome_unknown=True) from None
        return {"object_id": object_id, "accepted": True}

    def _check_target(self, object_id: str, action_id: str) -> wire.DeviceEntry:
        entry = self._lights.get(object_id)
        if entry is None:
            raise IntegrationError(ErrorCode.TARGET_MISSING, MISSING_LIGHT)
        if action_id not in wire.light_capabilities(entry.trait_names):
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, UNSUPPORTED_ACTION)
        if object_id in self._unverified_access:
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, ACCESS_UNVERIFIED)
        if action_id not in self._capabilities.get(object_id, ()):
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, UNSUPPORTED_ACTION)
        return entry

    @action("set_power")
    async def set_power(self, arguments: dict) -> dict:
        object_id = arguments["object_id"]
        entry = self._check_target(object_id, "set_power")
        await self._send_update(entry, wire.power_traits(arguments["on"]))
        self._pending_power[object_id] = arguments["on"]
        self._schedule_write_refresh(object_id)
        return {"object_id": object_id, "accepted": True}

    async def _send_update(self, entry: wire.DeviceEntry, traits: list) -> None:
        body = wire.single_update_request(entry.device_ref, traits)
        response = await self._call("update_traits", body, write=True)
        try:
            wire.verify_update_ack(response, entry.google_device_id, traits)
        except wire.ProtocolError as error:
            raise _protocol_error("write-ack", error, outcome_unknown=True) from None

    @action("toggle_power")
    async def toggle_power(self, arguments: dict) -> dict:
        object_id = arguments["object_id"]
        self._check_target(object_id, "set_power")
        states = await self.read_states([object_id])
        if object_id in self._pending_power:
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, TOGGLE_PENDING)
        if len(states) != 1 or not states[0].available or not isinstance(
                states[0].values.get("on"), bool):
            raise IntegrationError(ErrorCode.PROVIDER_ERROR, TOGGLE_STATE_UNKNOWN)
        return await self.set_power({"object_id": object_id,
                                     "on": not states[0].values["on"]})

    @action("set_brightness")
    async def set_brightness(self, arguments: dict) -> dict:
        object_id = arguments["object_id"]
        entry = self._check_target(object_id, "set_brightness")
        level = arguments["brightness_pct"]
        await self._send_update(entry, wire.set_level_traits(level))
        self._pending_power[object_id] = level > 0
        self._schedule_write_refresh(object_id)
        return {"object_id": object_id, "accepted": True}

    @action("set_color_temperature")
    async def set_color_temperature(self, arguments: dict) -> dict:
        object_id = arguments["object_id"]
        entry = self._check_target(object_id, "set_color_temperature")
        await self._send_update(entry, wire.color_temperature_traits(arguments["kelvin"]))
        self._schedule_write_refresh(object_id)
        return {"object_id": object_id, "accepted": True}

    @action("set_color")
    async def set_color(self, arguments: dict) -> dict:
        object_id = arguments["object_id"]
        entry = self._check_target(object_id, "set_color")
        await self._send_update(entry, wire.color_hsv_traits(
            arguments["hue_degrees"], arguments["saturation_pct"], arguments["value_pct"]))
        self._schedule_write_refresh(object_id)
        return {"object_id": object_id, "accepted": True}

    async def _open_browser(self, menu_id: str, arguments: dict) -> dict:
        self._menu_room = arguments.get("object_id")
        self._menu_offset = 0
        if self._menu_room is not None and self._menu_room not in self._rooms:
            raise IntegrationError(ErrorCode.TARGET_MISSING, "The selected room is unavailable.")
        if not await self._setup.ui.open_menu(menu_id, permission_id="google_home.menu"):
            raise IntegrationError(ErrorCode.INSTANCE_UNAVAILABLE, "The Google Home menu could not be opened.")
        return {}

    @action("browse_rooms")
    async def browse_rooms(self, arguments: dict) -> dict:
        return await self._open_browser("rooms", arguments)

    @action("browse_room")
    async def browse_room(self, arguments: dict) -> dict:
        return await self._open_browser("room_devices", arguments)

    @action("browse_devices")
    async def browse_devices(self, arguments: dict) -> dict:
        return await self._open_browser("devices", arguments)

    @action("browse_automations")
    async def browse_automations(self, arguments: dict) -> dict:
        return await self._open_browser("automations", arguments)

    async def menu_children(self, menu_id: str, page_id: str, cursor: str | None):
        if menu_id not in ("rooms", "room_devices", "devices", "automations"):
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "Unknown Google Home menu.")
        if menu_id == "automations":
            await self._refresh_automations()
            entries = [(key, entry.display_name, entry.enabled) for key, entry in self._automations.items()]
        elif menu_id == "rooms":
            entries = [(key, room.display_name, "toggle_room_power" in self._room_capabilities(key))
                       for key, room in self._rooms.items()]
        else:
            allowed = (set(self._rooms[self._menu_room].device_ids) if self._menu_room in self._rooms
                       else set()) if menu_id == "room_devices" else None
            entries = []
            for entry in self._devices.values():
                light_id = wire.object_id(self._structure_id, entry.google_device_id)
                if allowed is not None and entry.google_device_id not in allowed:
                    continue
                thermostat_id = wire.stable_id("thermostat", self._structure_id, entry.google_device_id)
                key = thermostat_id if thermostat_id in self._thermostats else light_id
                entries.append((key, entry.display_name,
                                bool(self._thermostat_caps.get(key)) if key in self._thermostats else
                                "toggle_power" in self._capabilities.get(key, ())))
        entries.sort(key=lambda item: (item[1].casefold(), item[0]))
        try:
            offset = int(cursor) if cursor is not None else (self._menu_offset if page_id != "root" else 0)
        except (TypeError, ValueError):
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "Invalid menu cursor.") from None
        if offset < 0 or offset > len(entries):
            raise IntegrationError(ErrorCode.INVALID_ARGUMENT, "Invalid menu cursor.")
        self._menu_offset = offset
        selected = entries[offset:offset + 25]
        self._menu_targets = {"i_" + key.split(":")[-1][:24]: key for key, _, _ in selected}
        items, pages = [], []
        for key, name, supported in selected:
            item_id = "i_" + key.split(":")[-1][:24]
            item = {"type": "action_item", "id": item_id, "label": _label(name),
                    "disabled": not supported}
            if menu_id in ("devices", "room_devices") and supported:
                # A single confirm always opens the device's submenu of every
                # control it has; a double press toggles it directly (a mappable
                # menu alternate), but only when it is actually toggleable.
                item["opens"] = "p_" + key.split(":")[-1][:24]
                pages.append({"type": "page", "id": item["opens"], "title": _label(name),
                              "lazy": True, "items": []})
                if "toggle_power" in self._capabilities.get(key, ()) \
                        or "toggle_thermostat" in self._thermostat_caps.get(key, ()):
                    item["alternates"] = {"double": {"id": "toggle", "label": "Toggle"}}
            items.append(item)
        title = {"rooms": "Rooms", "room_devices": "Room devices", "devices": "Home devices",
                 "automations": "Automations"}[menu_id]
        root = {"type": "page", "id": "root", "title": title, "items": items}
        if page_id != "root":
            target = self._menu_targets.get("i_" + page_id.removeprefix("p_"))
            if target not in self._lights and target not in self._thermostats:
                raise IntegrationError(ErrorCode.TARGET_MISSING, "The selected device is unavailable.")
            states = await self.read_states([target])
            values = states[0].values if states else {}
            unavailable = not states or not states[0].available
            controls = []
            caps = self._capabilities.get(target, self._thermostat_caps.get(target, ()))
            # The submenu always lists every control the device supports.
            if "toggle_power" in caps or "toggle_thermostat" in caps:
                on = values.get("on")
                if not isinstance(on, bool) and isinstance(values.get("mode"), str):
                    on = values["mode"] != "off"
                if isinstance(on, bool):
                    controls.append({"type": "toggle_item", "id": "power", "label": "Power", "on": on})
                else:
                    controls.append({"type": "action_item", "id": "power", "label": "Power unavailable", "disabled": True})
            if "set_brightness" in caps and isinstance(values.get("brightness_pct"), int):
                controls.append({"type": "slider_item", "id": "brightness", "label": "Brightness",
                                 "step": 1, "commit": "confirm", "visual": {"type": "bar", "id": "level",
                                 "min": 0, "max": 100, "value": values["brightness_pct"]}})
            if "set_color_temperature" in caps \
                    and isinstance(values.get("color_temperature_k"), int):
                controls.append({"type": "slider_item", "id": "color_temp", "label": "Color temperature (K)",
                                 "step": 100, "commit": "confirm", "visual": {"type": "bar", "id": "kelvin",
                                 "min": 1000, "max": 40000, "value": values["color_temperature_k"]}})
            if "set_color" in caps and all(
                    isinstance(values.get(field), (int, float))
                    for field in ("hue_degrees", "saturation_pct", "color_value_pct")):
                for item_id, label, field, maximum in (("hue", "Hue", "hue_degrees", 360),
                                                       ("saturation", "Saturation", "saturation_pct", 100),
                                                       ("color_value", "Color value", "color_value_pct", 100)):
                    controls.append({"type": "slider_item", "id": item_id, "label": label,
                                     "step": 1, "commit": "confirm", "visual": {"type": "bar", "id": item_id + "_bar",
                                     "min": 0, "max": maximum, "value": values[field]}})
            if "set_thermostat_temperature" in caps and isinstance(values.get("target_temperature_c"), (int, float)):
                low, high = wire.thermostat_range(self._thermostats[target])
                controls.append({"type": "slider_item", "id": "temperature", "label": "Temperature (C)",
                                 "step": 0.5, "commit": "confirm", "visual": {"type": "bar", "id": "temp",
                                 "min": low, "max": high, "value": max(low, min(high, values["target_temperature_c"]))}})
            if "set_thermostat_mode" in caps:
                modes = wire.thermostat_modes(self._thermostats[target])
                if values.get("mode") in modes:
                    controls.append({"type": "choice_item", "id": "mode", "label": "Mode",
                                     "options": [{"id": mode, "label": mode.replace("-", " ").replace("_", " ").capitalize()} for mode in modes],
                                     "selected": values["mode"]})
                else:
                    controls.append({"type": "action_item", "id": "mode", "label": "Mode unavailable", "disabled": True})
            if unavailable:
                for control in controls:
                    control["disabled"] = True
            for page in pages:
                if page["id"] == page_id:
                    page.update(lazy=False, items=controls, empty_label="No available controls")
        spec = {"v": 1, "surface": "menu", "size": "standard", "root": root, "pages": pages}
        return {"spec": spec, "next_cursor": str(offset + 25) if offset + 25 < len(entries) else None}

    def _toggle_device(self, key: str):
        """The toggle coroutine for a device key (thermostat or light)."""
        if key in self._thermostats:
            return self.toggle_thermostat({"object_id": key})
        return self.toggle_power({"object_id": key})

    def _menu_label(self, key: str) -> str:
        for store in (self._lights, self._thermostats):
            entry = store.get(key)
            if entry is not None:
                return _label(entry.display_name)
        room = self._rooms.get(key)
        if room is not None:
            return _label(room.display_name)
        automation = self._automations.get(key)
        if automation is not None:
            return _label(automation.display_name)
        return "Google Home"

    async def _menu_toast(self, key: str, action_label: str, phase: str) -> None:
        """A brief success/error toast for a control used from inside a menu.
        Menu events bypass the host's action-feedback path, so the provider
        shows this itself. Feedback must never break the control path."""
        if self._setup is None:
            return
        try:
            await self._setup.ui.toast({
                "v": 1, "toast_id": "google_home_menu", "size": "standard",
                "state": phase,
                "root": {"type": "column", "gap": 6, "children": [
                    {"type": "text", "role": "title", "max_lines": 1, "text": self._menu_label(key)},
                    {"type": "text", "role": "caption", "tone": "muted", "text": action_label},
                    {"type": "row", "gap": 6, "children": [
                        {"type": "status", "tone": phase, "pulse": phase == "pending"},
                        {"type": "text", "role": "caption", "tone": phase, "text": {
                            "pending": "Sending…", "success": "Request accepted",
                            "error": "Failed", "unknown": "Outcome unknown"}[phase]}]},
                    {"type": "progress", "tone": phase,
                     **({"indeterminate": True} if phase == "pending" else {"value": 1 if phase == "success" else 0})}]},
            }, permission_id="google_home.toast")
        except Exception:
            pass

    async def _menu_write(self, key: str, action_label: str, coro) -> None:
        self._toast_sequence += 1
        sequence = self._toast_sequence
        await self._menu_toast(key, action_label, "pending")
        try:
            await coro
        except Exception as error:
            if sequence == self._toast_sequence:
                await self._menu_toast(key, action_label,
                                       "unknown" if getattr(getattr(error, "info", error), "outcome_unknown", False) else "error")
            raise
        if sequence == self._toast_sequence:
            await self._menu_toast(key, action_label, "success")

    async def on_menu_event(self, menu_id: str, event: dict) -> None:
        kind = event.get("kind")
        if kind not in ("select", "alternate"):
            return
        item, page = event.get("item"), event.get("page")
        key = self._menu_targets.get(item if page == "root" else "i_" + str(page).removeprefix("p_"))
        if key is None:
            return
        if kind == "alternate":
            # Double-press a device to toggle it (declared alternate).
            if event.get("alternate") == "toggle":
                await self._menu_write(key, "Toggle", self._toggle_device(key))
            return
        if menu_id == "automations":
            await self._menu_write(key, "Run", self.run_automation({"object_id": key}))
        elif menu_id == "rooms":
            await self._menu_write(key, "Toggle room", self.toggle_room_power({"object_id": key}))
        elif item == "brightness":
            await self._menu_write(key, f"Brightness: {int(event['value'])}%", self.set_brightness(
                {"object_id": key, "brightness_pct": int(event["value"])}))
        elif item == "temperature":
            await self._menu_write(key, f"Temperature: {event['value']:g} °C", self.set_thermostat_temperature(
                {"object_id": key, "temperature_c": event["value"]}))
        elif item == "color_temp":
            await self._menu_write(key, f"Color temperature: {int(event['value'])} K", self.set_color_temperature(
                {"object_id": key, "kelvin": int(event["value"])}))
        elif item == "mode":
            await self._menu_write(key, f"Mode: {event['value'].replace('-', ' ').capitalize()}", self.set_thermostat_mode(
                {"object_id": key, "mode": event["value"]}))
        elif item in ("hue", "saturation", "color_value"):
            states = await self.read_states([key])
            values = states[0].values if states and states[0].available else {}
            fields = ("hue_degrees", "saturation_pct", "color_value_pct")
            if not all(isinstance(values.get(field), (int, float)) for field in fields):
                raise IntegrationError(ErrorCode.PROVIDER_ERROR, "The light's color state is unavailable.")
            color = dict(zip(("hue_degrees", "saturation_pct", "value_pct"), (values[field] for field in fields)))
            color[{"hue": "hue_degrees", "saturation": "saturation_pct", "color_value": "value_pct"}[item]] = event["value"]
            await self._menu_write(key, "Color", self.set_color({"object_id": key, **color}))
        elif item == "power":
            on = event.get("value")
            if isinstance(on, bool) and key in self._lights:
                command = self.set_power({"object_id": key, "on": on})
            elif isinstance(on, bool) and "set_thermostat_power" in self._thermostat_caps.get(key, ()):
                command = self.set_thermostat_power({"object_id": key, "on": on})
            else:
                command = self._toggle_device(key)
            await self._menu_write(key, "Turn on" if on is True else "Turn off" if on is False else "Toggle power", command)
