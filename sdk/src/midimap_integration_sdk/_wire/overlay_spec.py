"""Overlay specification v1: strict, bounded, transport-free data validation.

``validate_overlay_spec`` returns an owned JSON document. A toast envelope has
``v``, ``toast_id``, ``size`` and ``root``; ``surface`` defaults to ``toast``.
A menu surface instead uses a page root and may carry ``menu_id``. Other
envelope fields are optional. Containers use
``children``; menu pages use ``items``; slider items use ``visual``. Values are
plain JSON. ``apply_overlay_patch`` validates a complete candidate copy before
returning it, and never mutates the caller's document. The reserved patch key
``$toast`` addresses the envelope's dynamic ``state`` and ``tone`` fields;
null removes optional dynamic fields and is retained for required nullable
timeline values.

Manifest declarations, host services, attribution, asset I/O, scheduling,
and drawing are deliberately handled by later phases.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Callable

from . import policy
from .contracts import DomainError, ErrorCode, check_identifier, check_opaque_id, copy_json

Path = tuple[str | int, ...]
Validator = Callable[[Any, Path], None]
_ID = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_HEX = re.compile(r"#[0-9a-fA-F]{6}\Z")
TONES = frozenset({"neutral", "accent", "info", "success", "warning", "error",
                   "pending", "unknown", "muted"})
FORMATS = frozenset({"int", "1dp", "percent", "db", "kelvin", "mss", "hz"})
SIZES = frozenset({"compact", "standard", "wide"})
STATES = frozenset({"idle", "pending", "success", "error", "unknown"})
PRIORITIES = frozenset({"low", "normal", "high", "critical"})
SURFACES = frozenset({"toast", "menu"})


def _fail(path: Path, message: str, code: ErrorCode = ErrorCode.INVALID_ARGUMENT):
    raise DomainError(code, path, message)


def _object(value: Any, path: Path, required=(), optional=()):
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    if set(value) - set(required) - set(optional):
        _fail(path, "contains an unsupported field")
    for key in required:
        if key not in value:
            _fail(path + (key,), "is required")


def _string(value: Any, path: Path):
    if not isinstance(value, str) or len(value) > policy.OVERLAY_TEXT_MAX_CHARS:
        _fail(path, "must be text within the overlay text limit")
    if any((ord(c) < 32 and c != "\n") or 127 <= ord(c) < 160 for c in value):
        _fail(path, "must not contain control characters")


def _qr_payload(value: Any, path: Path):
    if not isinstance(value, str) or not 1 <= len(value) <= policy.OVERLAY_QR_PAYLOAD_MAX_CHARS \
            or any(not 32 <= ord(c) <= 126 for c in value):
        _fail(path, "must be nonempty printable ASCII within the QR payload limit")


def _id(value: Any, path: Path):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(path, "must be a lowercase overlay identifier of at most 32 characters")


def _reference_id(value: Any, path: Path):
    check_identifier(value, path)


def _opaque_id(value: Any, path: Path):
    check_opaque_id(value, path)


def _bool(value: Any, path: Path):
    if type(value) is not bool:
        _fail(path, "must be a boolean")


def _number(minimum=None, maximum=None, *, integer=False, nullable=False) -> Validator:
    def check(value, path):
        if nullable and value is None:
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or (integer and not isinstance(value, int)) \
                or (isinstance(value, float) and not math.isfinite(value)):
            _fail(path, "must be a finite " + ("integer" if integer else "number"))
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            _fail(path, "is outside the supported range")
    return check


def _enum(options) -> Validator:
    def check(value, path):
        if not isinstance(value, str) or value not in options:
            _fail(path, "is not a supported value")
    return check


def _color(value, path):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        _fail(path, "must be a #RRGGBB colour")


def _icon_name(value, path):
    _string(value, path)
    if not value or any(c.isspace() for c in value):
        _fail(path, "must identify an icon")
    if value.startswith(("asset:", "exe:")):
        _reference_id(value.split(":", 1)[1], path)
    elif not (re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value)
              or (len(value) <= 16 and any(ord(c) > 127 for c in value)
                  and not any(c in value for c in "/\\:"))):
        _fail(path, "must be an icon name, asset reference, executable reference, or emoji")


def _array(item_check: Validator, maximum: int, minimum: int = 0) -> Validator:
    def check(value, path):
        if not isinstance(value, list) or not minimum <= len(value) <= maximum:
            _fail(path, f"must be an array with {minimum} to {maximum} items")
        for index, item in enumerate(value):
            item_check(item, path + (index,))
    return check


_unit = _number(0, 1)
_scalar = _number(-1_000_000_000, 1_000_000_000)
_pixels = _number(0, 512)
_count = _number(0, 128, integer=True)
_tone = _enum(TONES)
_format = _enum(FORMATS)
_align = _enum({"start", "center", "end", "stretch"})


def _marker(value, path):
    _object(value, path, ("value",), ("tone", "style"))
    _scalar(value["value"], path + ("value",))
    if "tone" in value:
        _tone(value["tone"], path + ("tone",))
    if "style" in value:
        _enum({"line", "dot"})(value["style"], path + ("style",))


def _stop(value, path):
    _object(value, path, ("at", "color"))
    _unit(value["at"], path + ("at",))
    _color(value["color"], path + ("color",))


def _alternates(value, path):
    _object(value, path, (), ("double", "triple"))
    for key, item in value.items():
        _object(item, path + (key,), ("id", "label"))
        _reference_id(item["id"], path + (key, "id"))
        _string(item["label"], path + (key, "label"))


def _option(value, path):
    _object(value, path, ("id", "label"))
    _reference_id(value["id"], path + ("id",))
    _string(value["label"], path + ("label",))


_COMMON: dict[str, Validator] = {
    "id": _id, "gap": _number(0, 128), "pad": _number(0, 128),
    "align": _align, "grow": _number(0, 100), "tone": _tone,
}
_VALUE_FIELDS: dict[str, Validator] = {
    "min": _scalar, "max": _scalar, "value": _scalar, "origin": _scalar,
    "ghost": _scalar, "markers": _array(_marker, 32), "ticks": _count,
    "segments": _count, "gradient": _array(_stop, 16, 2),
    "preset": _enum({"kelvin", "hue"}), "thickness": _number(1, 128),
}
_ITEM_FIELDS: dict[str, Validator] = {
    "label": _string, "sublabel": _string, "icon": _icon_name, "trailing": _string,
    "chevron": _bool, "check": _bool, "alternates": _alternates, "disabled": _bool,
}

# Each entry is (required scalar fields, scalar field validators). Nested
# element fields are checked separately, so no unvalidated JSON can escape.
_SCHEMAS: dict[str, tuple[tuple[str, ...], dict[str, Validator]]] = {
    "column": ((), {}), "row": ((), {}), "stack": ((), {}),
    "grid": (("cols",), {"cols": _number(1, 16, integer=True)}),
    "card": ((), {"tone": _tone}), "spacer": ((), {"width": _pixels, "height": _pixels}),
    "divider": ((), {"orientation": _enum({"horizontal", "vertical"}), "tone": _tone}),
    "text": (("text",), {"text": _string, "role": _enum({"title", "subtitle", "body", "caption", "value", "mono"}),
                           "tone": _tone, "max_lines": _number(1, 8, integer=True), "ellipsis": _bool}),
    "readout": (("value",), {"value": _scalar, "unit": _string, "format": _format, "delta": _scalar, "tone": _tone}),
    "badge": (("text",), {"text": _string, "tone": _tone}),
    "kv": (("label", "value"), {"label": _string, "value": _string, "tone": _tone}),
    "hint": (("control", "label"), {"control": _enum({"nav", "select", "hold", "double", "triple"}), "label": _string}),
    "icon": (("name",), {"name": _icon_name, "tone": _tone, "size": _number(1, 512)}),
    "image": (("asset",), {"asset": _reference_id, "fit": _enum({"contain", "cover"}), "radius": _pixels,
                            "width": _pixels, "height": _pixels}),
    "swatch": ((), {"rgb": _color, "kelvin": _number(1000, 40000), "hue": _number(0, 360), "sat": _unit, "size": _pixels}),
    "status": ((), {"tone": _tone, "pulse": _bool, "size": _pixels}),
    "spinner": ((), {"tone": _tone, "size": _pixels}),
    "qr": (("text",), {"text": _qr_payload, "size": _pixels}),
    "bar": (("min", "max", "value"), {**_VALUE_FIELDS, "orientation": _enum({"horizontal", "vertical"})}),
    "dial": (("min", "max", "value"), {**_VALUE_FIELDS, "sweep": _number(1, 360), "center_label": _string, "size": _pixels}),
    "meter": (("levels",), {"levels": _array(_unit, 2, 1), "peak_hold_ms": _number(0, 60000, integer=True), "orientation": _enum({"horizontal", "vertical"})}),
    "steps": (("index", "count"), {"index": _number(0, 199, integer=True), "count": _number(1, 200, integer=True)}),
    "switch": (("on",), {"on": _bool}),
    "range": (("lo", "hi"), {"lo": _scalar, "hi": _scalar, "min": _scalar, "max": _scalar}),
    "progress": ((), {"value": _unit, "indeterminate": _bool, "tone": _tone}),
    "timeline": (("position", "duration"), {"position": _number(0, 1_000_000_000, nullable=True),
                                               "duration": _number(0, 1_000_000_000, nullable=True), "labels": _bool}),
    "sparkline": (("points",), {"points": _array(_scalar, policy.OVERLAY_SPARKLINE_POINTS_MAX, 1), "tone": _tone}),
    "countdown": (("remaining_ms",), {"remaining_ms": _number(0, policy.OVERLAY_DURATION_MAX_MS, integer=True), "format": _format}),
    "xy": (("x", "y"), {"x": _unit, "y": _unit}),
    "item": (("id", "label"), {**_ITEM_FIELDS, "opens": _id}),
    "toggle_item": (("id", "label", "on"), {**_ITEM_FIELDS, "on": _bool}),
    "slider_item": (("id", "label"), {**_ITEM_FIELDS, "step": _scalar, "fine_step": _scalar,
                                          "commit": _enum({"live", "confirm"})}),
    "choice_item": (("id", "label", "options", "selected"), {**_ITEM_FIELDS, "options": _array(_option, policy.MENU_PAGE_ITEMS_MAX, 1), "selected": _reference_id}),
    "action_item": (("id", "label"), {**_ITEM_FIELDS, "opens": _id, "confirm": _bool}),
    "section": (("label",), {"label": _string}),
    "page": (("id", "title"), {"title": _string, "lazy": _bool, "focus": _id, "empty_label": _string,
                                "total": _number(0, policy.MENU_TOTAL_ITEMS_MAX, integer=True)}),
}
CONTAINERS = frozenset({"column", "row", "stack", "grid", "card"})
MENU_TYPES = frozenset({"item", "toggle_item", "slider_item", "choice_item", "action_item", "section", "page"})
MENU_ITEM_TYPES = MENU_TYPES - {"page"}
VALUE_TYPES = frozenset({"bar", "dial", "meter", "steps", "switch", "range", "progress", "timeline", "sparkline", "countdown", "xy"})
SLIDER_VISUAL_TYPES = frozenset({"bar", "dial"})
ELEMENT_TYPES = frozenset(_SCHEMAS)

# Structural fields, IDs and fallback are intentionally absent.
_DYNAMIC: dict[str, frozenset[str]] = {
    "image": frozenset({"asset"}),
    "text": frozenset({"text", "tone"}), "readout": frozenset({"value", "unit", "delta", "tone"}),
    "badge": frozenset({"text", "tone"}), "kv": frozenset({"label", "value", "tone"}),
    "hint": frozenset({"label"}), "icon": frozenset({"name", "tone"}),
    "swatch": frozenset({"rgb", "kelvin", "hue", "sat"}), "status": frozenset({"tone", "pulse"}),
    "spinner": frozenset({"tone"}), "qr": frozenset({"text"}),
    "bar": frozenset({"value", "origin", "ghost", "markers", "gradient", "preset"}),
    "dial": frozenset({"value", "origin", "ghost", "markers", "gradient", "preset", "center_label"}),
    "meter": frozenset({"levels"}), "steps": frozenset({"index"}), "switch": frozenset({"on"}),
    "range": frozenset({"lo", "hi"}), "progress": frozenset({"value", "indeterminate", "tone"}),
    "timeline": frozenset({"position", "duration", "labels"}), "sparkline": frozenset({"points", "tone"}),
    "countdown": frozenset({"remaining_ms"}), "xy": frozenset({"x", "y"}),
    "item": frozenset({"label", "sublabel", "icon", "trailing", "chevron", "check", "disabled"}),
    "toggle_item": frozenset({"label", "sublabel", "icon", "trailing", "on", "disabled"}),
    "slider_item": frozenset({"label", "sublabel", "icon", "trailing", "disabled"}),
    "choice_item": frozenset({"label", "sublabel", "icon", "trailing", "selected", "disabled"}),
    "action_item": frozenset({"label", "sublabel", "icon", "trailing", "disabled", "confirm"}),
    "section": frozenset({"label"}), "page": frozenset({"title"}),
}


def _check_relations(element: dict, path: Path):
    kind = element["type"]
    if kind in {"bar", "dial"}:
        lo, hi = element["min"], element["max"]
        if lo >= hi:
            _fail(path, "minimum must be less than maximum")
        for key in ("value", "origin", "ghost"):
            if key in element and not lo <= element[key] <= hi:
                _fail(path + (key,), "must lie within the declared range")
        for i, marker in enumerate(element.get("markers", [])):
            if not lo <= marker["value"] <= hi:
                _fail(path + ("markers", i, "value"), "must lie within the declared range")
        stops = element.get("gradient", [])
        if any(a["at"] >= b["at"] for a, b in zip(stops, stops[1:])):
            _fail(path + ("gradient",), "stops must have increasing positions")
        if "gradient" in element and "preset" in element:
            _fail(path, "must choose either a gradient or a preset")
    elif kind == "swatch":
        modes = ("rgb" in element, "kelvin" in element, "hue" in element or "sat" in element)
        if sum(modes) != 1 or (("hue" in element) != ("sat" in element)):
            _fail(path, "must contain exactly one complete colour representation")
    elif kind == "steps" and element["index"] >= element["count"]:
        _fail(path + ("index",), "must be less than count")
    elif kind == "range":
        if element["lo"] > element["hi"] or ("min" in element and element["lo"] < element["min"]) \
                or ("max" in element and element["hi"] > element["max"]) \
                or ("min" in element and "max" in element and element["min"] >= element["max"]):
            _fail(path, "has an invalid range")
    elif kind == "progress":
        if ("value" in element) == (element.get("indeterminate") is True):
            _fail(path, "must have either a value or indeterminate state")
    elif kind == "timeline":
        if element["position"] is not None and element["duration"] is not None \
                and element["position"] > element["duration"]:
            _fail(path + ("position",), "must not exceed duration")
    elif kind == "choice_item":
        ids = [option["id"] for option in element["options"]]
        if len(ids) != len(set(ids)) or element["selected"] not in ids:
            _fail(path, "must select one unique declared option")
    elif kind == "page" and "total" in element:
        items = element.get("items", [])
        if not element.get("lazy", False) or (isinstance(items, list) and element["total"] < len(items)):
            _fail(path + ("total",), "requires a lazy page and must cover its initial items")


# count holds [visited, surface limit, current item's absolute limit or None].
# The item limit is checked during traversal, including visual/fallback nodes.
def _menu_items(items: Any, path: Path, depth: int, ids: set[str], count: list, *, limit: int):
    if not isinstance(items, list) or len(items) > limit:
        _fail(path, "must be a bounded menu item array")
    for i, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) \
                or item["type"] not in MENU_ITEM_TYPES:
            _fail(path + (i,), "must be a menu item")
        before = count[0]
        previous = count[2]
        count[2] = before + policy.MENU_ITEM_ELEMENTS_MAX
        try:
            _element(item, path + (i,), "menu", depth, ids, count)
        finally:
            count[2] = previous


def _element(element: Any, path: Path, surface: str, depth: int,
             ids: set[str], count: list, *, fallback=False):
    if depth > policy.OVERLAY_DEPTH_MAX:
        _fail(path, "exceeds the maximum element depth")
    count[0] += 1
    if count[0] > count[1]:
        _fail(path, "exceeds the maximum element count")
    if count[2] is not None and count[0] > count[2]:
        _fail(path, "exceeds the maximum menu subtree size")
    if not isinstance(element, dict):
        _fail(path, "must be an element object")
    kind = element.get("type")
    if not isinstance(kind, str) or kind not in _SCHEMAS:
        _fail(path + ("type",), "is not a supported element type")
    if kind in MENU_TYPES and (surface != "menu" or fallback):
        _fail(path, "menu elements require the menu surface and cannot be fallbacks")
    required, fields = _SCHEMAS[kind]
    nested = ({"children"} if kind in CONTAINERS else
              {"items"} if kind == "page" else {"visual"} if kind == "slider_item" else set())
    nested_required = nested if kind in CONTAINERS or kind == "slider_item" else set()
    _object(element, path, ("type", *required, *nested_required),
            (*_COMMON, *fields, *nested, "fallback"))
    for key, check in {**_COMMON, **fields}.items():
        if key in element:
            check(element[key], path + (key,))
    if "id" in element:
        if element["id"] in ids:
            _fail(path + ("id",), "must be unique within the specification")
        ids.add(element["id"])
    _check_relations(element, path)
    if "children" in nested:
        children = element["children"]
        if not isinstance(children, list) or len(children) > policy.OVERLAY_ELEMENTS_MAX:
            _fail(path + ("children",), "must be a bounded array")
        for i, child in enumerate(children):
            _element(child, path + ("children", i), surface, depth+1, ids, count, fallback=fallback)
    if kind == "page":
        items = element.get("items", [])
        _menu_items(items, path + ("items",), depth+1, ids, count,
                    limit=policy.MENU_PAGE_ITEMS_MAX)
        if "focus" in element and not element.get("lazy", False) and not any(
                item.get("id") == element["focus"] and item["type"] != "section" for item in items):
            _fail(path + ("focus",), "must identify an item on this page")
    if kind == "slider_item":
        visual = element["visual"]
        if not isinstance(visual, dict) or not isinstance(visual.get("type"), str) \
                or visual["type"] not in SLIDER_VISUAL_TYPES:
            _fail(path + ("visual",), "must be a bar or dial")
        _element(visual, path + ("visual",), surface, depth+1, ids, count)
        span = visual["max"] - visual["min"]
        for field in ("step", "fine_step"):
            if field in element and (element[field] <= 0 or
                    element[field] < span / policy.MENU_SLIDER_STEPS_MAX):
                _fail(path + (field,), "must be positive and produce at most 10000 steps")
        if element.get("fine_step", 0) > element.get("step", span / 100):
            _fail(path + ("fine_step",), "must not exceed step")
    if "fallback" in element:
        value = element["fallback"]
        if isinstance(value, str):
            _string(value, path + ("fallback",))
        else:
            previous = count[2]
            if kind == "page":
                count[2] = count[0] + policy.OVERLAY_ELEMENTS_MAX
            try:
                _element(value, path + ("fallback",), surface, depth+1, ids, count, fallback=True)
            finally:
                count[2] = previous


def _check_encoded_size(owned: Any, maximum: int):
    try:
        encoded = json.dumps(owned, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError, OverflowError):
        _fail((), "must be valid UTF-8 JSON")
    if len(encoded) > maximum:
        _fail((), "exceeds the maximum encoded specification size")


def validate_overlay_spec(spec: Any) -> dict:
    """Validate a v1 overlay and return a fresh JSON copy (never mutate input)."""
    owned = copy_json(spec)
    _object(owned, (), ("v", "size", "root"),
            ("toast_id", "menu_id", "surface", "tone", "state", "priority", "lifetime", "group", "accent", "pages"))
    if type(owned["v"]) is not int or owned["v"] not in policy.OVERLAY_SPEC_VERSIONS:
        _fail(("v",), "is not a supported overlay specification version", ErrorCode.INCOMPATIBLE_VERSION)
    _enum(SIZES)(owned["size"], ("size",))
    surface = owned.get("surface", "toast")
    _enum(SURFACES)(surface, ("surface",))
    _check_encoded_size(owned, policy.MENU_SPEC_MAX_BYTES if surface == "menu"
                        else policy.OVERLAY_SPEC_MAX_BYTES)
    if surface == "toast" and "toast_id" not in owned:
        _fail(("toast_id",), "is required for a toast")
    if surface == "toast" and "menu_id" in owned:
        _fail(("menu_id",), "requires the menu surface")
    if surface == "toast" and "pages" in owned:
        _fail(("pages",), "requires the menu surface")
    if "toast_id" in owned:
        _opaque_id(owned["toast_id"], ("toast_id",))
    if "menu_id" in owned:
        _reference_id(owned["menu_id"], ("menu_id",))
    for key, check in (("tone", _tone), ("state", _enum(STATES)), ("priority", _enum(PRIORITIES)),
                       ("group", _opaque_id), ("accent", _color)):
        if key in owned:
            check(owned[key], (key,))
    if "lifetime" in owned:
        lifetime = owned["lifetime"]
        _object(lifetime, ("lifetime",), (), ("duration_ms", "linger_ms", "sticky"))
        for key in ("duration_ms", "linger_ms"):
            if key in lifetime:
                _number(0, policy.OVERLAY_DURATION_MAX_MS, integer=True)(lifetime[key], ("lifetime", key))
        if "sticky" in lifetime:
            _bool(lifetime["sticky"], ("lifetime", "sticky"))
            if lifetime["sticky"] and not 0 < lifetime.get("duration_ms", 0) <= policy.OVERLAY_STICKY_MAX_MS:
                _fail(("lifetime",), "sticky toasts require a bounded duration")
    element_limit = policy.MENU_ELEMENTS_MAX if surface == "menu" else policy.OVERLAY_ELEMENTS_MAX
    ids, count = set(), [0, element_limit, None]
    _element(owned["root"], ("root",), surface, 1, ids, count)
    if surface == "menu" and owned["root"].get("type") != "page":
        _fail(("root",), "menu root must be a page")
    if surface == "menu":
        pages = owned.get("pages", [])
        if not isinstance(pages, list) or len(pages) > policy.MENU_PAGES_MAX:
            _fail(("pages",), "must be a bounded page array")
        for i, page in enumerate(pages):
            if not isinstance(page, dict) or page.get("type") != "page":
                _fail(("pages", i), "must be a page")
            _element(page, ("pages", i), surface, 1, ids, count)
        page_ids = {p["id"] for p in [owned["root"], *pages]}
        for page in [owned["root"], *pages]:
            for item in page.get("items", []):
                if "opens" in item and (item["opens"] not in page_ids or item["opens"] == page["id"]):
                    _fail(("pages",), "page link must name another declared page")
    return owned


def validate_menu_cursor(cursor: Any) -> str | None:
    """Validate an opaque paging cursor, or None for an initial/final page."""
    if cursor is None:
        return None
    return check_opaque_id(cursor, ("cursor",), policy.MENU_CURSOR_MAX_CHARS)


def validate_menu_fragment(fragment: Any, *, limit: int = policy.MENU_PAGE_ITEMS_MAX,
                           expected_page_id: str | None = None) -> dict:
    """Validate an owned page fragment against the requested page and limit.

    Fragment item IDs are unique within the fragment and cannot equal page_id.
    A caller combining fragments must also check IDs and cursor progress across
    fragments; that state belongs to the later menu controller.
    """
    _number(1, policy.MENU_PAGE_ITEMS_MAX, integer=True)(limit, ("limit",))
    if expected_page_id is not None:
        _id(expected_page_id, ("expected_page_id",))
    owned = copy_json(fragment)
    _object(owned, (), ("page_id", "items", "next_cursor"))
    _check_encoded_size(owned, policy.MENU_FRAGMENT_MAX_BYTES)
    _id(owned["page_id"], ("page_id",))
    if expected_page_id is not None and owned["page_id"] != expected_page_id:
        _fail(("page_id",), "does not match the requested page")
    if owned["next_cursor"] is not None:
        check_opaque_id(owned["next_cursor"], ("next_cursor",), policy.MENU_CURSOR_MAX_CHARS)
    _menu_items(owned["items"], ("items",), 2, {owned["page_id"]},
                [0, policy.MENU_ELEMENTS_MAX, None], limit=limit)
    for item in owned["items"]:
        if item.get("opens") == owned["page_id"]:
            _fail(("items",), "a page cannot link to itself")
    return owned


def apply_overlay_patch(spec: Any, patch: Any) -> dict:
    """Apply dynamic fields atomically to an owned copy, then revalidate it."""
    candidate = validate_overlay_spec(spec)
    change = copy_json(patch)
    is_menu = candidate.get("surface", "toast") == "menu"
    max_elements = policy.MENU_ELEMENTS_MAX if is_menu else policy.OVERLAY_ELEMENTS_MAX
    max_bytes = policy.MENU_SPEC_MAX_BYTES if is_menu else policy.OVERLAY_SPEC_MAX_BYTES
    # At most one entry per element plus the reserved envelope entry.
    if not isinstance(change, dict) or not change or len(change) > max_elements + 1:
        _fail((), "patch must be a nonempty bounded object")
    _check_encoded_size(change, max_bytes)
    index: dict[str, dict] = {}

    def visit(node):
        if "id" in node:
            index[node["id"]] = node
        for child in node.get("children", []):
            visit(child)
        for child in node.get("items", []):
            visit(child)
        if "visual" in node:
            visit(node["visual"])
        if isinstance(node.get("fallback"), dict):
            visit(node["fallback"])

    visit(candidate["root"])
    for page in candidate.get("pages", []):
        visit(page)
    for i, (element_id, fields) in enumerate(change.items()):
        if element_id == "$toast":
            if not isinstance(fields, dict) or not fields or set(fields) - {"state", "tone"}:
                _fail(("$toast",), "contains unsupported envelope patch fields")
            for field, value in fields.items():
                if value is None:
                    candidate.pop(field, None)
                else:
                    candidate[field] = value
            continue
        field_path = ("patch", i)
        _id(element_id, field_path)
        element = index.get(element_id)
        if element is None:
            _fail(field_path, "does not identify an element")
        allowed = _DYNAMIC.get(element["type"], frozenset()) | {"tone"}
        if not isinstance(fields, dict) or not fields or set(fields) - allowed:
            _fail(field_path, "contains unsupported or structural patch fields")
        required = _SCHEMAS[element["type"]][0]
        for field, value in fields.items():
            # Null removes an optional dynamic field; required nullable fields
            # such as timeline.position retain null as their actual value.
            if value is None and field not in required:
                element.pop(field, None)
            else:
                element[field] = value
    return validate_overlay_spec(candidate)
