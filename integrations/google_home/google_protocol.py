"""Google Home Foyer wire codec and message shapes (Google Home Lights, v1).

This module is the pure, transport-free protocol layer of the unofficial
Google Home Lights package. It encodes and decodes the JSON/protobuf array
form ("pblite") that Google's own Home web client uses, and builds the exact
discovery, state-read and command bodies the provider sends. It performs no
I/O, opens no socket, holds no credentials and never logs.

Evidence level. The typed-value codec (`encode_value` / `decode_value` and the
`number` / `boolean` / `parameter` / `trait` / `device_update` builders) is
fully determined by the web client's own serializers, which the plan recorded
directly: an array's element *i* carries protobuf field *i*+1, so field 1 is
index 0. The discovery and GetTraits *response* layouts (`parse_home_graph`,
`parse_get_traits`) are coded to the array indexes observed during planning;
those indexes are evidence, not a stable contract, so they live as named
constants here and are exercised only against synthetic fixtures until the
live qualification in the implementation plan confirms them. Where a shape was
not observed (room membership), nothing is invented: the label falls back to
the device's own display name.

pblite typed value (Google's `Value`-like `Nq`), by populated field:

    field 1 (index 0)  Null        the enum NULL_VALUE (0)
    field 2 (index 1)  Number      a finite JSON number
    field 3 (index 2)  String
    field 4 (index 3)  Boolean     the integer 0 or 1 (or a JSON bool)
    field 5 (index 4)  Struct      [ [[key, value], ...] ]   (its field 1 is the map)
    field 6 (index 5)  List        [ [value, ...] ]          (its field 1 is repeated)

Exactly one field is populated; an absent field is `null`. 0, False and ""
are values, not absence, and are preserved. A value with no populated field,
with more than one, with a non-finite number, or with an out-of-range boolean
is malformed and rejected.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ── Transport contract (the host broker maps these; see the plan, §2.1) ──────
RPC_BASE = "https://googlehomefoyer-pa.clients6.google.com/$rpc/"
STRUCTURES_SERVICE = "google.internal.home.foyer.v1.StructuresService"
HOME_CONTROL_SERVICE = "google.internal.home.foyer.v1.HomeControlService"
AUTOMATION_SERVICE = "google.internal.home.foyer.v1.AutomationService"
GET_HOME_GRAPH = f"{STRUCTURES_SERVICE}/GetHomeGraph"
GET_TRAITS = f"{HOME_CONTROL_SERVICE}/GetTraits"
UPDATE_TRAITS = f"{HOME_CONTROL_SERVICE}/UpdateTraits"
LIST_AUTOMATIONS = f"{AUTOMATION_SERVICE}/ListAutomations"
EXECUTE_AUTOMATION = f"{AUTOMATION_SERVICE}/ExecuteAutomation"
# The only operations the worker may ask the host to perform, each to a fixed
# path. The host never accepts an arbitrary RPC path from the package.
OPERATIONS: dict[str, str] = {
    "get_home_graph": GET_HOME_GRAPH,
    "get_traits": GET_TRAITS,
    "update_traits": UPDATE_TRAITS,
    "list_automations": LIST_AUTOMATIONS,
    "execute_automation": EXECUTE_AUTOMATION,
}
CONTENT_TYPE = "application/json+protobuf"

# ── Google type and trait names ──────────────────────────────────────────────
LIGHT_TYPE = "action.devices.types.LIGHT"
DEVICE_GROUP_TYPE = "action.devices.types.DEVICE_GROUP"
ROUTINE_TYPE = "action.devices.types.ROUTINE"
THERMOSTAT_TYPE = "action.devices.types.THERMOSTAT"
AC_UNIT_TYPE = "action.devices.types.AC_UNIT"
TRAIT_ON_OFF = "onOff"
TRAIT_BRIGHTNESS = "brightness"
TRAIT_DEVICE_STATUS = "deviceStatus"
TRAIT_COLOR = "color"
TRAIT_TEMPERATURE_SETTING = "temperatureSetting"
# GetHomeGraph uses Cloud-to-cloud trait identifiers; GetTraits uses the
# shorter Foyer names above. Normalize only the exact identifiers qualified
# for this package, leaving unfamiliar declarations unchanged.
GRAPH_TRAIT_ALIASES = {
    "action.devices.traits.OnOff": TRAIT_ON_OFF,
    "action.devices.traits.Brightness": TRAIT_BRIGHTNESS,
    "action.devices.traits.ColorSetting": TRAIT_COLOR,
    "action.devices.traits.TemperatureSetting": TRAIT_TEMPERATURE_SETTING,
}

BRIGHTNESS_MIN = 0
BRIGHTNESS_MAX = 100


class ProtocolError(Exception):
    """A Google message did not have a shape this codec accepts.

    Carries no Google content: the message text is a fixed description only.
    """


# ── Typed-value encoders (the web client's own serializers) ──────────────────

def null() -> list:
    """The pblite `Null` value (field 1 = the NULL_VALUE enum, 0)."""
    return [0]


def number(n: int | float) -> list:
    """A pblite number value (field 2). Rejects bool and non-finite numbers."""
    if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n):
        raise ProtocolError("a number value must be a finite number")
    return [None, n]


def string(s: str) -> list:
    """A pblite string value (field 3)."""
    if not isinstance(s, str):
        raise ProtocolError("a string value must be text")
    return [None, None, s]


def boolean(b: bool) -> list:
    """A pblite boolean value (field 4), encoded as the client does: 0 or 1."""
    if not isinstance(b, bool):
        raise ProtocolError("a boolean value must be true or false")
    return [None, None, None, 1 if b else 0]


def struct(mapping: Mapping[str, Any]) -> list:
    """A pblite struct value (field 5) from a plain mapping of JSON values."""
    if not isinstance(mapping, Mapping):
        raise ProtocolError("a struct value must be a mapping")
    pairs = [[key, encode_value(value)] for key, value in mapping.items()]
    return [None, None, None, None, [pairs]]


def list_value(items: Sequence[Any]) -> list:
    """A pblite list value (field 6) from a plain sequence of JSON values."""
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise ProtocolError("a list value must be a sequence")
    return [None, None, None, None, None, [[encode_value(item) for item in items]]]


def encode_value(value: Any) -> list:
    """The pblite typed value for a plain JSON value (round-trips `decode_value`)."""
    if value is None:
        return null()
    if isinstance(value, bool):
        return boolean(value)
    if isinstance(value, (int, float)):
        return number(value)
    if isinstance(value, str):
        return string(value)
    if isinstance(value, Mapping):
        return struct(value)
    if isinstance(value, Sequence):
        return list_value(value)
    raise ProtocolError("value is not a JSON value")


# ── Trait / parameter / device-update encoders (the web client's builders) ───

def parameter(name: str, value: list) -> list:
    """One trait parameter: its name (field 1) and its typed value (field 2)."""
    if not isinstance(name, str) or not name:
        raise ProtocolError("a parameter needs a name")
    return [name, value]


def trait(name: str, parameters: Sequence[list]) -> list:
    """One trait: its name (field 1) and its parameters (field 2)."""
    if not isinstance(name, str) or not name:
        raise ProtocolError("a trait needs a name")
    return [name, list(parameters)]


def device_update(device_ref: Sequence[Any], traits: Sequence[list]) -> list:
    """One device's update: its complete reference (field 1) and traits (field 2).

    `device_ref` is the opaque reference discovery returned for the device; it
    is passed through unchanged so its provider identity is never discarded.
    """
    return [list(device_ref), list(traits)]


# ── Command builders (the plan, §2.5) ────────────────────────────────────────

def power_traits(on: bool) -> list:
    """Traits that set onOff to `on`."""
    return [trait(TRAIT_ON_OFF, [parameter(TRAIT_ON_OFF, boolean(on))])]


def brightness_traits(brightness_pct: int) -> list:
    """Traits that turn the light on and set its brightness (percent 1..100).

    A positive brightness implies power on; the two traits are sent together.
    Physical atomicity is a live-qualification question, not an assumption.
    """
    _check_brightness(brightness_pct)
    if brightness_pct <= 0:
        raise ProtocolError("use set_level_traits for brightness 0 (power off)")
    return [trait(TRAIT_ON_OFF, [parameter(TRAIT_ON_OFF, boolean(True))]),
            trait(TRAIT_BRIGHTNESS, [parameter(TRAIT_BRIGHTNESS, number(brightness_pct))])]


def set_level_traits(brightness_pct: int) -> list:
    """Traits for an absolute level: 0 is an explicit power-off, >0 is on at that level.

    This is the user-visible brightness semantics the plan requires; a positive
    value means on at that brightness, zero means off.
    """
    _check_brightness(brightness_pct)
    return power_traits(False) if brightness_pct <= 0 else brightness_traits(brightness_pct)


def color_temperature_traits(kelvin: int) -> list:
    if isinstance(kelvin, bool) or not isinstance(kelvin, int) or not 1000 <= kelvin <= 40000:
        raise ProtocolError("color temperature must be between 1000 and 40000 kelvin")
    return [trait(TRAIT_COLOR, [parameter("colorTemperature", number(kelvin))])]


def color_hsv_traits(hue: int | float, saturation: int | float, value: int | float) -> list:
    if any(not _finite_number(v) for v in (hue, saturation, value)) \
            or not 0 <= hue <= 360 or not 0 <= saturation <= 100 or not 0 <= value <= 100:
        raise ProtocolError("color values are outside their allowed range")
    return [trait(TRAIT_COLOR, [parameter("spectrumHsv", struct({
        "hue": hue, "saturation": saturation / 100, "value": value / 100}))])]


def thermostat_mode_traits(mode: str) -> list:
    if not isinstance(mode, str) or not mode or len(mode) > 32:
        raise ProtocolError("a thermostat mode must be short nonempty text")
    return [trait(TRAIT_TEMPERATURE_SETTING, [parameter("mode", string(mode))])]


def thermostat_temperature_traits(temperature_c: int | float) -> list:
    if isinstance(temperature_c, bool) or not isinstance(temperature_c, (int, float)) \
            or not math.isfinite(temperature_c):
        raise ProtocolError("a thermostat setpoint must be a finite number")
    return [trait(TRAIT_TEMPERATURE_SETTING, [
        parameter("thermostatTemperatureSetpoint", number(temperature_c))])]


def list_automations_request(structure_id: str) -> list:
    if not isinstance(structure_id, str) or not structure_id:
        raise ProtocolError("a structure id must be text")
    return [structure_id, True]


def execute_automation_request(structure_id: str, automation_id: str, backend: int) -> list:
    if not isinstance(structure_id, str) or not structure_id \
            or not isinstance(automation_id, str) or not automation_id:
        raise ProtocolError("automation identifiers must be text")
    if isinstance(backend, bool) or not isinstance(backend, int):
        raise ProtocolError("an automation backend must be numeric")
    return [structure_id, automation_id, None, 2, backend == 3]


def update_traits_request(updates: Sequence[list]) -> list:
    """An UpdateTraits body: one field, a list of device updates."""
    return [list(updates)]


def single_update_request(device_ref: Sequence[Any], traits: Sequence[list]) -> list:
    """An UpdateTraits body for one device's traits."""
    return update_traits_request([device_update(device_ref, traits)])


def get_home_graph_request() -> list:
    """The minimal GetHomeGraph body verified during planning: an empty array."""
    return []


def get_traits_request(google_device_ids: Sequence[str]) -> list:
    """A GetTraits body: one field, a list of single-element device-id arrays.

    At most 50 devices per request (`GET_TRAITS_BATCH_MAX`); the caller batches.
    """
    ids = list(google_device_ids)
    if len(ids) > GET_TRAITS_BATCH_MAX:
        raise ProtocolError("too many devices in one GetTraits request")
    for device_id in ids:
        if not isinstance(device_id, str) or not device_id:
            raise ProtocolError("a device id must be text")
    return [[[device_id] for device_id in ids]]


GET_TRAITS_BATCH_MAX = 50


def serialize(body: Any) -> str:
    """The exact request text: compact JSON with no NaN/Infinity, as Google's client sends."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ── Typed-value decoder ──────────────────────────────────────────────────────

def decode_value(node: Any) -> Any:
    """The plain JSON value a pblite typed value carries.

    Exactly one field must be populated. Raises `ProtocolError` for an empty
    value, a conflicting value (more than one field populated), a non-finite
    number, an out-of-range boolean, or a malformed struct/list.
    """
    if not isinstance(node, list):
        raise ProtocolError("a typed value must be an array")
    present = [index for index in range(min(len(node), 6)) if node[index] is not None]
    if len(node) > 6 and any(item is not None for item in node[6:]):
        raise ProtocolError("a typed value has an unknown populated field")
    if len(present) != 1:
        raise ProtocolError("a typed value must have exactly one populated field")
    index = present[0]
    raw = node[index]
    if index == 0:                                     # Null (the NULL_VALUE enum, 0)
        if isinstance(raw, bool) or raw != 0:
            raise ProtocolError("a null value must be the enum 0")
        return None
    if index == 1:                                     # Number
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            raise ProtocolError("a number value must be a finite number")
        return raw
    if index == 2:                                     # String
        if not isinstance(raw, str):
            raise ProtocolError("a string value must be text")
        return raw
    if index == 3:                                     # Boolean (0/1 or a JSON bool)
        if isinstance(raw, bool):
            return raw
        if raw in (0, 1):
            return bool(raw)
        raise ProtocolError("a boolean value must be 0 or 1")
    if index == 4:                                     # Struct
        return _decode_struct(raw)
    return _decode_list(raw)                           # index == 5: List


def _decode_struct(node: Any) -> dict:
    if not isinstance(node, list):
        raise ProtocolError("a struct must be an array")
    pairs = node[0] if node and node[0] is not None else []
    if not isinstance(pairs, list):
        raise ProtocolError("a struct's fields must be an array")
    result: dict = {}
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) < 2 or not isinstance(pair[0], str):
            raise ProtocolError("a struct field must be a [key, value] pair")
        if pair[0] in result:
            raise ProtocolError("a struct has a duplicate field name")
        result[pair[0]] = decode_value(pair[1])
    return result


def _decode_list(node: Any) -> list:
    if not isinstance(node, list):
        raise ProtocolError("a list must be an array")
    values = node[0] if node and node[0] is not None else []
    if not isinstance(values, list):
        raise ProtocolError("a list's values must be an array")
    return [decode_value(value) for value in values]


# ── Discovery (GetHomeGraph response) ────────────────────────────────────────
# Zero-based array indexes observed during planning (evidence, not a contract).
IDX_STRUCTURES = 1              # response[1] = the structures
IDX_STRUCTURE_ID = 0           # structure[0] = the Google structure id
IDX_STRUCTURE_NAME = 1         # structure[1] = the home name
IDX_STRUCTURE_ROOMS = 5        # structure[5] = rooms
IDX_STRUCTURE_DEVICES = 6      # structure[6] = devices
IDX_DEVICE_REF = 0             # device[0] = the complete device reference
IDX_DEVICE_NAME = 3            # device[3] = the display name
IDX_DEVICE_TYPE = 5            # device[5] = the device type
IDX_DEVICE_TRAITS = 6          # device[6] = the declared traits
IDX_DEVICE_ATTRS = 19          # device[19] = attributes, when present
IDX_DEVICE_ID_IN_REF = 0       # device_ref[0] = the Google device id
IDX_ROOM_ID = 0
IDX_ROOM_NAME = 2
IDX_ROOM_DEVICES = 4


@dataclass(frozen=True)
class RoomEntry:
    room_id: str
    display_name: str
    structure_id: str
    home_name: str
    device_ids: tuple[str, ...]


@dataclass(frozen=True)
class DeviceEntry:
    """One device from the home graph, with its opaque reference and room."""
    device_ref: tuple
    google_device_id: str
    display_name: str
    device_type: str
    trait_names: tuple[str, ...]
    structure_id: str
    home_name: str
    room: str | None = None
    room_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_light(self) -> bool:
        return self.device_type == LIGHT_TYPE

    @property
    def is_group(self) -> bool:
        return self.device_type == DEVICE_GROUP_TYPE

    @property
    def is_routine(self) -> bool:
        return (self.device_type in (ROUTINE_TYPE, "action.devices.types.SCENE")
                or any(name in self.trait_names for name in
                       ("routine", "action.devices.traits.Routine", "action.devices.traits.Scene")))

    @property
    def is_thermostat(self) -> bool:
        return TRAIT_TEMPERATURE_SETTING in self.trait_names


def _at(node: Any, index: int) -> Any:
    """`node[index]` when `node` is a list long enough, else None."""
    return node[index] if isinstance(node, list) and index < len(node) else None


def _trait_names(declared: Any) -> tuple[str, ...]:
    """Normalized trait names from device[6] (graph and Foyer spellings)."""
    names: list[str] = []
    if isinstance(declared, list):
        for item in declared:
            if isinstance(item, str):
                names.append(GRAPH_TRAIT_ALIASES.get(item, item))
            elif isinstance(item, list) and item and isinstance(item[0], str):
                names.append(GRAPH_TRAIT_ALIASES.get(item[0], item[0]))
    return tuple(names)


def _device_attributes(raw: Any) -> Mapping[str, Any]:
    if raw is None:
        return {}
    try:
        return _decode_struct(raw)
    except ProtocolError:
        return {}


def parse_home_rooms(response: Any) -> list[RoomEntry]:
    rooms: list[RoomEntry] = []
    structures = _at(response, IDX_STRUCTURES)
    if not isinstance(structures, list):
        return rooms
    for structure in structures:
        structure_id = _at(structure, IDX_STRUCTURE_ID)
        home_name = _at(structure, IDX_STRUCTURE_NAME)
        raw_rooms = _at(structure, IDX_STRUCTURE_ROOMS)
        if not isinstance(structure_id, str) or not isinstance(raw_rooms, list):
            continue
        for room in raw_rooms:
            room_id = _at(room, IDX_ROOM_ID)
            if not isinstance(room_id, str) or not room_id:
                continue
            name = _at(room, IDX_ROOM_NAME)
            ids: list[str] = []
            raw_devices = _at(room, IDX_ROOM_DEVICES)
            if isinstance(raw_devices, list):
                for room_device in raw_devices:
                    ref = _at(room_device, 0)
                    device_id = _at(ref, IDX_DEVICE_ID_IN_REF)
                    if isinstance(device_id, str) and device_id and device_id not in ids:
                        ids.append(device_id)
            rooms.append(RoomEntry(room_id, name if isinstance(name, str) and name else "Room",
                                   structure_id,
                                   home_name if isinstance(home_name, str) else "", tuple(ids)))
    return rooms


def parse_home_graph(response: Any) -> list[DeviceEntry]:
    """Every device in the response, tagged with its structure and room."""
    entries: list[DeviceEntry] = []
    room_by_device: dict[tuple[str, str], RoomEntry] = {}
    for room in parse_home_rooms(response):
        for device_id in room.device_ids:
            room_by_device.setdefault((room.structure_id, device_id), room)
    structures = _at(response, IDX_STRUCTURES)
    if not isinstance(structures, list):
        return entries
    for structure in structures:
        structure_id = _at(structure, IDX_STRUCTURE_ID)
        home_name = _at(structure, IDX_STRUCTURE_NAME)
        devices = _at(structure, IDX_STRUCTURE_DEVICES)
        if not isinstance(structure_id, str) or not isinstance(devices, list):
            continue
        for device in devices:
            ref = _at(device, IDX_DEVICE_REF)
            if not isinstance(ref, list) or not ref:
                continue
            google_device_id = ref[IDX_DEVICE_ID_IN_REF]
            if not isinstance(google_device_id, str) or not google_device_id:
                continue
            name = _at(device, IDX_DEVICE_NAME)
            device_type = _at(device, IDX_DEVICE_TYPE)
            room = room_by_device.get((structure_id, google_device_id))
            entries.append(DeviceEntry(
                device_ref=tuple(ref), google_device_id=google_device_id,
                display_name=name if isinstance(name, str) and name else google_device_id,
                device_type=device_type if isinstance(device_type, str) else "",
                trait_names=_trait_names(_at(device, IDX_DEVICE_TRAITS)),
                structure_id=structure_id,
                home_name=home_name if isinstance(home_name, str) else "",
                room=room.display_name if room is not None else None,
                room_id=room.room_id if room is not None else None,
                attributes=_device_attributes(_at(device, IDX_DEVICE_ATTRS))))
    return entries


def lights(entries: Sequence[DeviceEntry], structure_id: str) -> list[DeviceEntry]:
    """The lights of one selected structure, excluding device groups."""
    return [entry for entry in entries
            if entry.structure_id == structure_id and entry.is_light
            and not entry.is_group and not entry.is_routine]


def thermostats(entries: Sequence[DeviceEntry], structure_id: str) -> list[DeviceEntry]:
    return [entry for entry in entries
            if entry.structure_id == structure_id and entry.is_thermostat
            and not entry.is_group and not entry.is_routine]


def stable_id(kind: str, structure_id: str, google_id: str) -> str:
    digest = hashlib.sha256(f"{structure_id}\x00{google_id}".encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def object_id(structure_id: str, google_device_id: str) -> str:
    """Keep the original light IDs stable for existing mappings."""
    return stable_id("light", structure_id, google_device_id)


def light_capabilities(trait_names: Sequence[str],
                       attributes: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, ...]:
    """Write actions supported by declared traits and their access attributes.

    `attributes` is keyed by trait name. An explicitly query-only trait cannot
    be used for a write even when the graph declares that trait. Missing
    attributes leave the declared-trait behavior unchanged.
    """
    traits = set(trait_names)
    attributes = attributes or {}
    power = TRAIT_ON_OFF in traits and attributes.get(TRAIT_ON_OFF, {}).get(
        "queryOnlyOnOff") is not True
    brightness = TRAIT_BRIGHTNESS in traits and attributes.get(TRAIT_BRIGHTNESS, {}).get(
        "queryOnlyBrightness") is not True
    caps: list[str] = []
    if power:
        caps.append("set_power")
        caps.append("toggle_power")
    if power and brightness:
        caps.append("set_brightness")
    if TRAIT_COLOR in traits and attributes.get(TRAIT_COLOR, {}).get("queryOnlyColorSetting") is not True:
        caps.extend(("set_color_temperature", "set_color"))
    return tuple(caps)


def thermostat_modes(entry: DeviceEntry) -> tuple[str, ...]:
    """Preserve Google's advertised mode IDs, including e.g. fan-only."""
    raw = entry.attributes.get("availableModes", entry.attributes.get("availableThermostatModes", []))
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(mode for mode in raw if isinstance(mode, str)
                              and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", mode)))


def thermostat_range(entry: DeviceEntry) -> tuple[float, float]:
    limits = entry.attributes.get("thermostatTemperatureRange", {})
    if isinstance(limits, Mapping):
        low, high = limits.get("minThresholdCelsius"), limits.get("maxThresholdCelsius")
        if _finite_number(low) and _finite_number(high) and low < high:
            return low, high
    return 5, 40


def thermostat_capabilities(entry: DeviceEntry,
                            attributes: Mapping[str, Mapping[str, Any]] | None = None,
                            ) -> tuple[str, ...]:
    access = attributes or {}
    caps = []
    power_access = access.get(TRAIT_ON_OFF, {})
    power = TRAIT_ON_OFF in entry.trait_names and power_access.get("queryOnlyOnOff") is not True
    if power:
        caps.append("set_thermostat_power")
    temperature = access.get(TRAIT_TEMPERATURE_SETTING, {}).get("queryOnlyTemperatureSetting") is not True
    if temperature:
        caps.append("set_thermostat_temperature")
    modes = thermostat_modes(entry)
    if modes and temperature:
        caps.append("set_thermostat_mode")
    if ((power and power_access.get("commandOnlyOnOff") is not True)
            or (temperature and "off" in modes and len(modes) > 1)):
        caps.append("toggle_thermostat")
    return tuple(caps)


# ── State (GetTraits response) ───────────────────────────────────────────────
IDX_GET_TRAITS_RECORDS = 0     # response[0] = the device records
IDX_RECORD_REF = 0             # record[0] = the device reference
IDX_RECORD_TRAITS = 1          # record[1] = the trait list


def decode_traits(traits: Any) -> dict[str, dict]:
    """`{trait_name: {parameter_name: value}}` from a trait list.

    Each trait is `[name, [parameter, ...]]`; each parameter is
    `[name, typed_value]`. Duplicate trait or parameter names are malformed.
    """
    if not isinstance(traits, list):
        raise ProtocolError("a trait list must be an array")
    result: dict[str, dict] = {}
    for item in traits:
        if not isinstance(item, list) or len(item) < 2 or not isinstance(item[0], str):
            raise ProtocolError("a trait must be a [name, parameters] pair")
        name, params = item[0], item[1]
        if name in result:
            raise ProtocolError("a device reported a trait twice")
        if not isinstance(params, list):
            raise ProtocolError("a trait's parameters must be an array")
        values: dict = {}
        for param in params:
            if not isinstance(param, list) or len(param) < 2 or not isinstance(param[0], str):
                raise ProtocolError("a parameter must be a [name, value] pair")
            if param[0] in values:
                raise ProtocolError("a trait reported a parameter twice")
            values[param[0]] = decode_value(param[1])
        result[name] = values
    return result


@dataclass(frozen=True)
class DeviceState:
    """Normalized last-observed state of one device.

    `available` is True only when Google explicitly reported the device online;
    a missing or false `online` leaves it False. `values` holds only the fields
    Google supplied (`on`, `brightness_pct`, and `color_temperature_k` when
    present); nothing is manufactured.
    """
    google_device_id: str
    values: dict
    available: bool


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def normalize_state(google_device_id: str, trait_map: Mapping[str, Mapping]) -> DeviceState:
    """Normalize only observed values; missing online state remains unavailable."""
    values: dict = {}
    on = trait_map.get(TRAIT_ON_OFF, {}).get(TRAIT_ON_OFF)
    if isinstance(on, bool):
        values["on"] = on
    brightness = trait_map.get(TRAIT_BRIGHTNESS, {}).get(TRAIT_BRIGHTNESS)
    if _finite_number(brightness) and 0 <= brightness <= 100:
        values["brightness_pct"] = int(brightness)
    color_values = trait_map.get(TRAIT_COLOR, {})
    color = color_values.get("colorTemperature")
    if _finite_number(color) and 1000 <= color <= 40000:
        values["color_temperature_k"] = int(color)
    spectrum = color_values.get("spectrumHsv")
    if isinstance(spectrum, Mapping):
        hue, saturation, value = spectrum.get("hue"), spectrum.get("saturation"), spectrum.get("value")
        if _finite_number(hue) and 0 <= hue <= 360:
            values["hue_degrees"] = hue
        if _finite_number(saturation) and 0 <= saturation <= 1:
            values["saturation_pct"] = round(saturation * 100, 2)
        if _finite_number(value) and 0 <= value <= 1:
            values["color_value_pct"] = round(value * 100, 2)
    temperature = trait_map.get(TRAIT_TEMPERATURE_SETTING, {})
    mode = temperature.get("mode")
    if isinstance(mode, str) and mode:
        values["mode"] = mode[:32]
    unit = temperature.get("temperatureUnit")
    if unit in ("C", "F"):
        values["temperature_unit"] = unit
    if _finite_number(temperature.get("thermostatTemperatureSetpointC")):
        setpoint = temperature["thermostatTemperatureSetpointC"]
    elif _finite_number(temperature.get("thermostatTemperatureSetpointF")):
        setpoint = (temperature["thermostatTemperatureSetpointF"] - 32) * 5 / 9
    elif _finite_number(temperature.get("thermostatTemperatureSetpoint")):
        setpoint = temperature["thermostatTemperatureSetpoint"]
    else:
        setpoint = None
    if setpoint is not None:
        values["target_temperature_c"] = round(setpoint, 2)
    ambient = temperature.get("thermostatTemperatureAmbient")
    if _finite_number(ambient):
        values["ambient_temperature_c"] = round(ambient, 2)
    online = trait_map.get(TRAIT_DEVICE_STATUS, {}).get("online")
    return DeviceState(google_device_id, values, available=online is True)


def parse_get_traits(response: Any) -> dict[str, DeviceState]:
    """`{google_device_id: DeviceState}` from a GetTraits response.

    Each record is `[device_ref, traits]`; the device id is the reference's
    first field. A record without a usable reference is skipped.
    """
    records = _at(response, IDX_GET_TRAITS_RECORDS)
    if not isinstance(records, list):
        raise ProtocolError("a GetTraits response must hold device records")
    states: dict[str, DeviceState] = {}
    for record in records:
        ref = _at(record, IDX_RECORD_REF)
        if not isinstance(ref, list) or not ref or not isinstance(ref[IDX_DEVICE_ID_IN_REF], str):
            continue
        google_device_id = ref[IDX_DEVICE_ID_IN_REF]
        trait_map = decode_traits(_at(record, IDX_RECORD_TRAITS) or [])
        states[google_device_id] = normalize_state(google_device_id, trait_map)
    return states


def parse_trait_access(response: Any) -> dict[str, dict[str, dict[str, Any]] | None]:
    """Trait access flags in the optional third field of GetTraits traits.

    The result is keyed by device ID, then trait name, then attribute name.
    Missing attribute fields are empty. An unfamiliar per-device access shape
    produces None for that device: discovery and state reads can continue, but
    the provider must not advertise write actions for it. A malformed top-level
    response remains a protocol error.
    """
    records = _at(response, IDX_GET_TRAITS_RECORDS)
    if not isinstance(records, list):
        raise ProtocolError("a GetTraits response must hold device records")
    result: dict[str, dict[str, dict[str, Any]] | None] = {}
    for record in records:
        ref = _at(record, IDX_RECORD_REF)
        if not isinstance(ref, list) or not ref or not isinstance(ref[0], str):
            continue
        traits = _at(record, IDX_RECORD_TRAITS)
        if not isinstance(traits, list):
            result[ref[0]] = None
            continue
        by_trait: dict[str, dict[str, Any]] = {}
        try:
            for item in traits:
                if not isinstance(item, list) or not item or not isinstance(item[0], str):
                    raise ProtocolError("a trait must have a name")
                attrs = _at(item, 2)
                decoded: dict[str, Any] = {}
                if attrs is not None:
                    if not isinstance(attrs, list):
                        raise ProtocolError("trait attributes must be an array")
                    # The live web response wraps repeated attribute records
                    # in their field-1 container: [[attribute, ...]]. Earlier
                    # synthetic fixtures used the unwrapped repeated list.
                    if (len(attrs) == 1 and isinstance(attrs[0], list)
                            and all(isinstance(part, list) for part in attrs[0])):
                        attrs = attrs[0]
                    for attr in attrs:
                        if not isinstance(attr, list) or len(attr) < 2 or not isinstance(attr[0], str):
                            raise ProtocolError("a trait attribute is malformed")
                        if attr[0] in decoded:
                            raise ProtocolError("a trait attribute is repeated")
                        decoded[attr[0]] = decode_value(attr[1])
                by_trait[item[0]] = decoded
        except ProtocolError:
            result[ref[0]] = None
        else:
            result[ref[0]] = by_trait
    return result


@dataclass(frozen=True)
class AutomationEntry:
    automation_id: str
    display_name: str
    enabled: bool
    starters: str
    actions: str
    backend: int
    structure_id: str


def parse_automations(response: Any, structure_id: str) -> list[AutomationEntry]:
    rows = _at(response, 0)
    if rows is None and response == []:
        rows = []
    if not isinstance(rows, list):
        raise ProtocolError("an automation list must contain rows")
    result: list[AutomationEntry] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        automation_id = row[0]
        if not isinstance(automation_id, str) or not automation_id:
            continue
        if automation_id in seen:
            raise ProtocolError("the automation list repeated an automation")
        seen.add(automation_id)
        enabled = row[2] is True or (not isinstance(row[2], bool) and row[2] == 1)
        name = row[3] if isinstance(row[3], str) and row[3] else "Unnamed automation"
        starters = row[4] if isinstance(row[4], str) else ""
        actions = row[5] if isinstance(row[5], str) else ""
        backend = row[20] if len(row) > 20 and isinstance(row[20], int) \
            and not isinstance(row[20], bool) else 0
        result.append(AutomationEntry(automation_id, name, enabled, starters, actions,
                                      backend, structure_id))
    return result


def verify_automation_ack(response: Any) -> None:
    if not isinstance(response, list) or not response or isinstance(response[0], bool) \
            or response[0] != 1:
        raise ProtocolError("the automation was not accepted")


def verify_update_acks(response: Any,
                       requested_by_device: Mapping[str, Sequence[list]]) -> None:
    """Require a matching, error-free acknowledgement for every requested target."""
    if not isinstance(response, list) or len(response) != 1 \
            or not isinstance(response[0], list):
        raise ProtocolError("an UpdateTraits acknowledgement has unexpected records")
    records = response[0]
    if len(records) != len(requested_by_device) or not requested_by_device:
        raise ProtocolError("an UpdateTraits acknowledgement has unexpected records")
    access = parse_trait_access(response)
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, list) or len(record) != 2:
            raise ProtocolError("an UpdateTraits acknowledgement has unexpected fields")
        ref, traits = record
        if not isinstance(ref, list) or len(ref) != 1 or not isinstance(ref[0], str) \
                or ref[0] not in requested_by_device or ref[0] in seen:
            raise ProtocolError("an UpdateTraits acknowledgement targets another device")
        google_device_id = ref[0]
        seen.add(google_device_id)
        reported = decode_traits(traits)
        requested = decode_traits(list(requested_by_device[google_device_id]))
        for name, params in reported.items():
            if any(marker in name.lower() for marker in ("error", "challenge", "failure")) \
                    or any(any(marker in key.lower() for marker in
                               ("error", "challenge", "failure", "status"))
                           for key in params):
                raise ProtocolError("an UpdateTraits acknowledgement reports an error")
        if reported.get(TRAIT_DEVICE_STATUS, {}).get("online") is False:
            raise ProtocolError("an UpdateTraits acknowledgement reports an offline device")
        for name, params in requested.items():
            if name not in reported or any(reported[name].get(key) != value
                                           for key, value in params.items()):
                raise ProtocolError("an UpdateTraits acknowledgement does not match the request")
        device_access = access.get(google_device_id)
        if device_access is None or any(
                any(marker in key.lower() for marker in
                    ("error", "challenge", "failure", "status"))
                for attrs in device_access.values() for key in attrs):
            raise ProtocolError("an UpdateTraits acknowledgement has unknown access metadata")
    if seen != set(requested_by_device):
        raise ProtocolError("an UpdateTraits acknowledgement has unexpected records")


def verify_update_ack(response: Any, google_device_id: str,
                      requested_traits: Sequence[list]) -> None:
    verify_update_acks(response, {google_device_id: requested_traits})


def _check_brightness(brightness_pct: Any) -> None:
    if isinstance(brightness_pct, bool) or not isinstance(brightness_pct, int) \
            or not BRIGHTNESS_MIN <= brightness_pct <= BRIGHTNESS_MAX:
        raise ProtocolError("brightness must be a whole percent from 0 to 100")
