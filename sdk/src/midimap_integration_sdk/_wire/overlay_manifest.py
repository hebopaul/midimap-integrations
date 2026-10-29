"""Format-3 UI declarations and template bindings; stdlib-only wire code."""
from collections.abc import Mapping
import re
from .contracts import DomainError, ErrorCode, check_identifier, check_text, copy_json
from .overlay_spec import validate_overlay_spec

_BINDING = re.compile(r"\$(?:args\.[a-zA-Z_][a-zA-Z0-9_]*|midi\.value|binding\.normalized|state\.[a-zA-Z_][a-zA-Z0-9_]*|feedback\.(?:status|tone|pending|progress)|outcome|action\.name|instance\.label|target\.label)\Z")


def _shape(value, required, optional=()):
    if not isinstance(value, Mapping) or not set(required) <= set(value) or set(value)-set(required)-set(optional):
        raise DomainError(ErrorCode.PACKAGE_INVALID, (), "UI declaration has invalid fields")


def resolve_template(spec, context=None):
    """Bindings are exact objects with an explicit, type-checked default.

    Never interpolate text or execute expressions. Missing observed state uses
    its declared default; all resolved data is validated as an ordinary v1 spec.
    """
    def walk(value):
        if isinstance(value, Mapping):
            if "$bind" in value:
                _shape(value, ("$bind", "default"))
                binding = value["$bind"]
                if not isinstance(binding, str) or not _BINDING.fullmatch(binding):
                    raise DomainError(ErrorCode.PACKAGE_INVALID, (), "unsupported UI binding")
                found = (context or {}).get(binding, value["default"])
                return copy_json(found)
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return [walk(v) for v in value]
        return value
    return validate_overlay_spec(walk(spec))


def validate_ui(value):
    _shape(value, ("spec_version",), ("accent", "icons", "templates", "menus"))
    if type(value["spec_version"]) is not int or value["spec_version"] != 1:
        raise DomainError(ErrorCode.INCOMPATIBLE_VERSION, ("ui", "spec_version"), "unsupported overlay version")
    from .validation import check_relative_path
    owned = copy_json(value)
    if "accent" in owned:
        validate_overlay_spec({"v": 1, "toast_id": "check", "size": "standard", "accent": owned["accent"],
                               "root": {"type": "spacer"}})
    for collection in ("icons", "templates", "menus"):
        entries = owned.get(collection, [])
        if not isinstance(entries, list) or len(entries) > 32:
            raise DomainError(ErrorCode.PACKAGE_INVALID, ("ui", collection), "must contain at most 32 entries")
        seen = set()
        for entry in entries:
            required = ("id", "path") if collection == "icons" else ("id", "spec") if collection == "templates" else ("id", "title", "spec")
            _shape(entry, required, ("dynamic",) if collection == "menus" else ())
            check_identifier(entry["id"], ("ui", collection, "id"))
            if entry["id"] in seen:
                raise DomainError(ErrorCode.PACKAGE_INVALID, ("ui", collection), "duplicate UI ID")
            seen.add(entry["id"])
            if collection == "icons":
                check_relative_path(entry["path"], ("ui", collection, "path"))
                if not entry["path"].lower().endswith(".png"):
                    raise DomainError(ErrorCode.PACKAGE_INVALID, (), "UI icons must be PNG")
            else:
                resolved = resolve_template(entry["spec"])
                if collection == "templates" and resolved.get("surface", "toast") != "toast":
                    raise DomainError(ErrorCode.PACKAGE_INVALID, (), "feedback templates must use toast surface")
                if collection == "menus":
                    check_text(entry["title"], ("ui", collection, "title"))
                    if resolved.get("surface") != "menu" or type(entry.get("dynamic", False)) is not bool:
                        raise DomainError(ErrorCode.PACKAGE_INVALID, (), "invalid menu declaration")
    return owned


def validate_feedback(value):
    _shape(value, ("template", "show_on"))
    check_identifier(value["template"], ("feedback", "template"))
    phases = value["show_on"]
    if not isinstance(phases, (list, tuple)) or not phases or any(not isinstance(p, str) for p in phases) or len(set(phases)) != len(phases) or not set(phases) <= {"intent", "success", "error", "unknown"}:
        raise DomainError(ErrorCode.PACKAGE_INVALID, ("feedback", "show_on"), "invalid feedback phases")
    return copy_json(value)
