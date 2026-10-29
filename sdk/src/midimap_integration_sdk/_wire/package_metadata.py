"""Format-4 package metadata validation. Pure wire contract shared with the SDK."""
from collections.abc import Mapping
import re

from .contracts import DomainError, ErrorCode, check_identifier, check_version, copy_json


def fail(message):
    raise DomainError(ErrorCode.PACKAGE_INVALID, (), message)


def shape(value, required, optional=()):
    if not isinstance(value, Mapping) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        fail("package metadata has invalid fields")


def compatibility(value):
    shape(value, ("min_midimap_version",), ("min_midimap_build", "capabilities"))
    result = copy_json(value)
    check_version(result["min_midimap_version"], ("compatibility", "min_midimap_version"))
    build = result.get("min_midimap_build", "0")
    if not isinstance(build, str) or re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", build) is None:
        fail("minimum MIDImap build must be a decimal string")
    capabilities = result.get("capabilities", [])
    if not isinstance(capabilities, list) or len(capabilities) > 64:
        fail("required capabilities must be a bounded list")
    for entry in capabilities:
        check_identifier(entry, ("compatibility", "capabilities"))
    if len(set(capabilities)) != len(capabilities):
        fail("required capabilities must be unique")
    return result


def color(value):
    if not isinstance(value, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", value) is None:
        fail("card colors must use #RRGGBB")


def card(value, icons):
    shape(value, ("icon",), ("accent", "gradient", "angle"))
    result = copy_json(value)
    check_identifier(result["icon"], ("card", "icon"))
    if result["icon"] not in icons:
        fail("the card icon must name a declared package icon")
    if "accent" in result:
        color(result["accent"])
    stops = result.get("gradient")
    if stops is not None:
        if not isinstance(stops, list) or not 2 <= len(stops) <= 16:
            fail("card gradients need 2 to 16 stops")
        positions = []
        for stop in stops:
            shape(stop, ("position", "color"))
            position = stop["position"]
            if isinstance(position, bool) or not isinstance(position, (int, float)) or not 0 <= position <= 1:
                fail("gradient positions must be between 0 and 1")
            color(stop["color"])
            positions.append(position)
        if positions[0] != 0 or positions[-1] != 1 or any(a >= b for a, b in zip(positions, positions[1:])):
            fail("gradient stops must increase from 0 to 1")
    angle = result.get("angle", 135)
    if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not 0 <= angle <= 360:
        fail("gradient angle must be between 0 and 360 degrees")
    return result


def operations(value):
    if not isinstance(value, (list, tuple)) or len(value) > 128:
        fail("migration operations must be a bounded list")
    for operation in value:
        if not isinstance(operation, Mapping):
            fail("invalid migration operation")
        op = operation.get("op")
        if not isinstance(op, str):
            fail("unsupported migration operation")
        keys = {"rename": ("op", "from", "to"), "copy": ("op", "from", "to"),
                "default": ("op", "to", "value"), "remove": ("op", "from")}.get(op)
        if keys is None:
            fail("unsupported migration operation")
        shape(operation, keys)
        for name in ("from", "to"):
            if name in operation:
                check_identifier(operation[name], ("migrations", name))
    return copy_json(value)


def migrations(value):
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        fail("migrations must be a bounded list")
    result, seen = [], set()
    for migration in value:
        shape(migration, ("from_version", "config"), ("actions",))
        version = check_version(migration["from_version"], ("migrations", "from_version"))
        if version in seen:
            fail("only one migration may apply to a previous version")
        seen.add(version)
        item = {"from_version": version, "config": operations(migration["config"])}
        actions = migration.get("actions", [])
        if not isinstance(actions, list) or len(actions) > 128:
            fail("action migrations must be a bounded list")
        sources, owned = set(), []
        for action in actions:
            shape(action, ("from", "to"), ("arguments",))
            for field in ("from", "to"):
                check_identifier(action[field], ("migrations", "actions", field))
            if action["from"] in sources:
                fail("action migration sources must be unique")
            sources.add(action["from"])
            owned.append({"from": action["from"], "to": action["to"],
                          "arguments": operations(action.get("arguments", []))})
        if owned:
            item["actions"] = owned
        result.append(item)
    return result
