"""Strict decoding and validation of manifests, schemas, arguments and messages.

Two layers, kept separate:

* Wire validation (`decode_message`, `parse_method_result`) checks envelope and
  method/event shapes without knowing which package is running.
* Domain validation (`validate_arguments`, `validate_result`,
  `validate_discovery_page`, `validate_grants`, `check_admission`, ...) checks
  declared action/permission IDs, schemas, package compatibility and lifecycle.

Author schemas are a documented JSON Schema Draft 2020-12 subset. This module
checks that subset itself and then delegates instance validation to the pinned
`jsonschema` package with an empty reference registry, so no remote schema can
be fetched. `jsonschema` is imported lazily on first use, keeping imports here
free of side effects. Validation never executes package code, never mutates a
caller's value and returns fresh copies the caller owns.
"""
from __future__ import annotations

import functools
import json
import re
from contextlib import contextmanager
from typing import Any

from . import policy
from .contracts import (
    HOST_ONLY_ERROR_CODES, REQUEST_ID_PREFIXES, ActionDefinition, ActionDispatch,
    CollectionDefinition,
    DiscoveryPage, DomainError, ErrorCode, ErrorInfo, Event, EventDefinition, Grant,
    GrantSet, InstanceRecord, LifecycleState, Message, ObjectEvent, ObjectRecord,
    PackageManifest, PackagePin, Path, PermissionDefinition, Request, Response,
    SecretDefinition, StateRecord, check_bool, check_identifier, check_int, check_opaque_id,
    check_package_id, check_request_id, check_secret_value, check_text, check_unique,
    check_version, copy_json, is_json_int, thaw_json,
    AuthDefinition, BrowserMethod, DeviceMethod, check_scope_token, check_secret_value_text,
    ConnectionDefinition, GrantDecision, WebSessionDefinition, check_endpoint,
)

_BOM = b"\xef\xbb\xbf"


@contextmanager
def _boundary(code: ErrorCode, prefix: Path = ()):
    """Relocate model errors below `prefix` and give them this boundary's code.

    Errors that already carry a specific code (for example a version mismatch)
    keep it; generic INVALID_ARGUMENT errors from models are recoded.
    """
    try:
        yield
    except DomainError as error:
        recode = code if error.code == ErrorCode.INVALID_ARGUMENT else None
        raise error.at(prefix, recode) from None


def _fail(path: Path, message: str, code: ErrorCode = ErrorCode.INVALID_ARGUMENT) -> DomainError:
    return DomainError(code, path, message)


def _fields(value: Any, path: Path, required: tuple[str, ...],
            optional: tuple[str, ...] = ()) -> dict:
    """Require an object with exactly these fields; unknown fields are rejected."""
    if not isinstance(value, dict):
        raise _fail(path, "must be an object")
    if set(value) - set(required) - set(optional):
        raise _fail(path, "contains an unsupported field")
    for name in required:
        if name not in value:
            raise _fail(path + (name,), "is required")
    return value


def _list(value: Any, path: Path, max_items: int | None = None) -> list:
    if not isinstance(value, list):
        raise _fail(path, "must be an array")
    if max_items is not None and len(value) > max_items:
        raise _fail(path, f"must contain at most {max_items} items")
    return value


def _object(value: Any, path: Path) -> dict:
    if not isinstance(value, dict):
        raise _fail(path, "must be an object")
    return value


# ── Strict JSON ─────────────────────────────────────────────────────────────

class _DuplicateKey(Exception):
    pass


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _reject_constant(_name: str) -> Any:
    raise ValueError("nonfinite")


def decode_json(raw: Any, *, max_bytes: int | None = None,
                code: ErrorCode = ErrorCode.INVALID_ARGUMENT) -> Any:
    """Decode one UTF-8 JSON document strictly.

    Rejects a BOM, invalid UTF-8, duplicate keys, NaN/Infinity (including
    overflowing literals), nesting beyond policy, trailing data and oversize
    input. Error messages describe positions, never the content.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise _fail((), "must be UTF-8 encoded bytes", code)
    if max_bytes is not None and len(raw) > max_bytes:
        raise _fail((), f"exceeds the maximum size of {max_bytes} bytes", code)
    if raw.startswith(_BOM):
        raise _fail((), "must not start with a byte order mark", code)
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as error:
        raise _fail((), f"is not valid UTF-8 (byte {error.start})", code) from None
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicates,
                           parse_constant=_reject_constant)
    except _DuplicateKey:
        raise _fail((), "contains a duplicate field name", code) from None
    except json.JSONDecodeError as error:
        raise _fail((), f"is not valid JSON (line {error.lineno}, column {error.colno})",
                    code) from None
    except RecursionError:
        raise _fail((), f"exceeds the maximum nesting depth of {policy.JSON_DEPTH_MAX}",
                    code) from None
    except ValueError:                      # NaN/Infinity or an oversized integer
        raise _fail((), "contains a number that is not supported", code) from None
    with _boundary(code):
        return copy_json(value)


# ── Author schema subset ────────────────────────────────────────────────────

_TYPE_KEYWORDS = {
    "object": {"properties", "required", "additionalProperties"},
    "array": {"items", "minItems", "maxItems"},
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum"},
    "number": {"minimum", "maximum"},
    "boolean": set(),
    "null": set(),
}
_COMMON_KEYWORDS = {"type", "enum", "title", "description"}
_SCHEMA_KEYWORDS = _COMMON_KEYWORDS.union(*_TYPE_KEYWORDS.values(), {"$defs", "$ref"})
_LOCAL_REF = re.compile(r"#/\$defs/([^/~]+)")
_ENUM_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": is_json_int,
    "number": lambda v: is_json_int(v) or isinstance(v, float),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def check_schema(schema: Any, path: Path = (), *, object_root: bool = False,
                 closed_root: bool = False,
                 code: ErrorCode = ErrorCode.PACKAGE_INVALID) -> dict:
    """Validate an author schema against the supported subset; return a copy.

    Supported: `type` (one of object, array, string, boolean, integer, number,
    null; no unions), `properties`, `required`, `additionalProperties` (boolean,
    mandatory on objects), `items` (mandatory on arrays), `minItems`,
    `maxItems`, `minLength`, `maxLength`, `minimum`, `maximum`, scalar `enum`,
    root `$defs`, local `$ref` to `#/$defs/<name>`, `title` and `description`.
    Anything else is rejected rather than ignored. References must be acyclic
    and the expanded schema may not exceed the JSON depth limit.
    """
    with _boundary(code, path):
        copied = copy_json(thaw_json(schema))
        _SchemaChecker(copied).check_root(object_root=object_root, closed_root=closed_root)
    return copied


class _SchemaChecker:
    def __init__(self, root: Any):
        self.root = root
        defs = root.get("$defs", {}) if isinstance(root, dict) else {}
        self.defs = defs if isinstance(defs, dict) else {}
        self.depths: dict[str, int] = {}
        self.visiting: list[str] = []

    def check_root(self, *, object_root: bool, closed_root: bool) -> None:
        root = self.root
        if not isinstance(root, dict):
            raise _fail((), "must be a schema object")
        if "$defs" in root:
            defs = _object(root["$defs"], ("$defs",))
            for name, definition in defs.items():
                check_identifier(name, ("$defs", name))
                self.node(definition, ("$defs", name))
        self.node(root, (), is_root=True)
        if object_root and (root.get("type") != "object" or "$ref" in root):
            raise _fail(("type",), "must be 'object' at the schema root")
        if closed_root and root.get("additionalProperties") is not False:
            raise _fail(("additionalProperties",), "must be false at the schema root")
        for name in self.defs:
            self.def_depth(name, 0)
        if self.depth(root, 0) > policy.JSON_DEPTH_MAX:
            raise self.too_deep()

    def node(self, node: Any, path: Path, is_root: bool = False) -> None:
        if not isinstance(node, dict):
            raise _fail(path, "must be a schema object")
        for keyword in node:
            if keyword not in _SCHEMA_KEYWORDS:
                raise _fail(path + (keyword,), "is not a supported schema keyword")
        if "$defs" in node and not is_root:
            raise _fail(path + ("$defs",), "is only supported at the schema root")
        for keyword, limit in (("title", policy.DISPLAY_NAME_MAX_CHARS),
                               ("description", policy.DESCRIPTION_MAX_CHARS)):
            if keyword in node:
                check_text(node[keyword], path + (keyword,), limit)
        if "$ref" in node:
            if set(node) - {"$ref", "title", "description"} - ({"$defs"} if is_root else set()):
                raise _fail(path, "must not combine '$ref' with other validation keywords")
            match = _LOCAL_REF.fullmatch(node["$ref"]) if isinstance(node["$ref"], str) else None
            if not match or match.group(1) not in self.defs:
                raise _fail(path + ("$ref",), "must reference a definition in '#/$defs'")
            return
        kind = node.get("type")
        if "type" not in node:
            raise _fail(path, "must declare a 'type'")
        if not isinstance(kind, str) or kind not in _TYPE_KEYWORDS:
            raise _fail(path + ("type",), "must be one supported type name")
        allowed = _COMMON_KEYWORDS | _TYPE_KEYWORDS[kind] | ({"$defs"} if is_root else set())
        for keyword in node:
            if keyword not in allowed:
                raise _fail(path + (keyword,), f"does not apply to type '{kind}'")
        getattr(self, "type_" + kind)(node, path)
        if "enum" in node:
            self.enum(node["enum"], kind, path + ("enum",))

    def type_object(self, node: dict, path: Path) -> None:
        if "additionalProperties" not in node:
            raise _fail(path + ("additionalProperties",), "is required on object schemas")
        check_bool(node["additionalProperties"], path + ("additionalProperties",))
        properties = _object(node.get("properties", {}), path + ("properties",))
        for name, child in properties.items():
            self.node(child, path + ("properties", name))
        if "required" in node:
            required = _list(node["required"], path + ("required",))
            for i, name in enumerate(required):
                if not isinstance(name, str) or name not in properties:
                    raise _fail(path + ("required", i), "must name a declared property")
            check_unique(tuple(required), path + ("required",), "property")

    def type_array(self, node: dict, path: Path) -> None:
        if "items" not in node:
            raise _fail(path + ("items",), "is required on array schemas")
        self.node(node["items"], path + ("items",))
        self.bounds(node, path, "minItems", "maxItems", check_count)

    def type_string(self, node: dict, path: Path) -> None:
        self.bounds(node, path, "minLength", "maxLength", check_count)

    def type_integer(self, node: dict, path: Path) -> None:
        self.bounds(node, path, "minimum", "maximum", check_int)

    def type_number(self, node: dict, path: Path) -> None:
        self.bounds(node, path, "minimum", "maximum", check_number)

    def type_boolean(self, node: dict, path: Path) -> None:
        pass

    def type_null(self, node: dict, path: Path) -> None:
        pass

    @staticmethod
    def bounds(node: dict, path: Path, low: str, high: str, check) -> None:
        for keyword in (low, high):
            if keyword in node:
                check(node[keyword], path + (keyword,))
        if low in node and high in node and node[low] > node[high]:
            raise _fail(path + (high,), f"must not be less than '{low}'")

    @staticmethod
    def enum(values: Any, kind: str, path: Path) -> None:
        if kind not in _ENUM_TYPES:
            raise _fail(path, "is only supported for scalar types")
        _list(values, path)
        if not values:
            raise _fail(path, "must not be empty")
        keys = []
        for i, value in enumerate(values):
            if not _ENUM_TYPES[kind](value):
                raise _fail(path + (i,), f"must be of type '{kind}'")
            keys.append(("bool", value) if isinstance(value, bool) else ("value", value))
        check_unique(tuple(keys), path, "value")

    # Depth of the schema with references expanded. Each reference hop counts
    # as a level, which also bounds recursion through long reference chains.
    def depth(self, node: dict, base: int) -> int:
        if base > policy.JSON_DEPTH_MAX:
            raise self.too_deep()
        if "$ref" in node:
            return self.def_depth(_LOCAL_REF.fullmatch(node["$ref"]).group(1), base)
        children = list(node.get("properties", {}).values())
        if "items" in node:
            children.append(node["items"])
        return 1 + max((self.depth(child, base + 1) for child in children), default=0)

    def def_depth(self, name: str, base: int) -> int:
        if name in self.visiting:
            raise _fail(("$defs", name), "is part of a reference cycle")
        if name not in self.depths:
            self.visiting.append(name)
            self.depths[name] = 1 + self.depth(self.defs[name], base + 1)
            self.visiting.pop()
        return self.depths[name]

    @staticmethod
    def too_deep() -> DomainError:
        return _fail((), f"expands beyond the maximum depth of {policy.JSON_DEPTH_MAX}")


def check_count(value: Any, path: Path) -> int:
    return check_int(value, path, 0)


def check_number(value: Any, path: Path) -> int | float:
    if not (is_json_int(value) or isinstance(value, float)):
        raise _fail(path, "must be a number")
    return value


# ── Instance validation (delegated to jsonschema) ───────────────────────────

@functools.cache
def _validator_factory():
    """Draft 2020-12 validator whose `integer` excludes floats and booleans.

    The empty registry has no retrieval function: references outside the
    schema document cannot resolve, so nothing is fetched remotely.
    """
    try:
        from jsonschema import Draft202012Validator, validators
        from referencing import Registry
    except ImportError:
        return None
    checker = Draft202012Validator.TYPE_CHECKER.redefine(
        "integer", lambda _checker, value: is_json_int(value))
    cls = validators.extend(Draft202012Validator, type_checker=checker)
    registry = Registry()
    return lambda schema: cls(schema, registry=registry)


def _error_message(error) -> tuple[Path, str]:
    """Safe path/description for a jsonschema error; never echoes the value."""
    path = tuple(error.absolute_path)
    keyword, expected = error.validator, error.validator_value
    if keyword == "required":
        instance = error.instance if isinstance(error.instance, dict) else {}
        missing = next((name for name in expected if name not in instance), None)
        return path + ((missing,) if missing else ()), "is required"
    return path, {
        "type": f"must be of type '{expected}'",
        "additionalProperties": "contains a field that is not allowed",
        "enum": "must be one of the allowed values",
        "minimum": f"must be at least {expected}",
        "maximum": f"must be at most {expected}",
        "minLength": f"must contain at least {expected} characters",
        "maxLength": f"must contain at most {expected} characters",
        "minItems": f"must contain at least {expected} items",
        "maxItems": f"must contain at most {expected} items",
    }.get(keyword, "is invalid")


def _path_order(error) -> tuple:
    return tuple((0, p) if isinstance(p, int) else (1, p) for p in error.absolute_path)


def validate_value(schema: Any, value: Any, *, code: ErrorCode,
                   partial: bool = False) -> Any:
    """Validate a JSON value against an accepted schema; return a private copy.

    `partial` accepts missing fields listed in the root `required` (draft
    configuration); supplied fields, including nested ones, remain fully checked.
    """
    factory = _validator_factory()
    if factory is None:
        raise RuntimeError("Integration validation requires the 'jsonschema' package "
                           "(install requirements-integrations.txt).")
    with _boundary(code):
        copied = copy_json(value)
    errors = [e for e in factory(thaw_json(schema)).iter_errors(copied)
              if not (partial and e.validator == "required" and not e.absolute_path)]
    if errors:
        path, message = _error_message(min(errors, key=_path_order))
        raise _fail(path, message, code)
    return copied


def validate_arguments(action: ActionDefinition, values: Any) -> dict:
    """Validated copy of action arguments; unknown fields and coercions fail."""
    return validate_value(action.input_schema, values, code=ErrorCode.INVALID_ARGUMENT)


def validate_result(action: ActionDefinition, value: Any) -> Any:
    """Validated copy of a worker's action result; run before reporting success."""
    return validate_value(action.output_schema, value, code=ErrorCode.PROTOCOL_ERROR)


def validate_config(manifest: PackageManifest, config: Any, *, draft: bool = False) -> dict:
    """Validated copy of nonsecret instance configuration.

    Drafts may omit required root fields; every supplied field must be valid.
    """
    return validate_value(manifest.config_schema, config,
                          code=ErrorCode.INVALID_ARGUMENT, partial=draft)


# ── Manifests ───────────────────────────────────────────────────────────────

_MANIFEST_FIELDS = (
    "format_version", "protocol_version", "id", "version", "name", "author",
    "description", "target", "entrypoint", "config_schema_version", "config_schema",
    "permissions", "actions", "collections", "events",
)
_RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul",
                             *(f"com{i}" for i in range(1, 10)),
                             *(f"lpt{i}" for i in range(1, 10))})
_UNSAFE_PATH_CHARS = re.compile(r'[\x00-\x1f<>:"\\|?*]')


def check_relative_path(value: Any, path: Path,
                        max_chars: int = policy.IDENTIFIER_MAX_CHARS) -> str:
    """Archive-relative POSIX path that is safe to use below a Windows root.

    Rejects absolute, drive and UNC forms, backslashes, empty, '.' and '..'
    segments, alternate data streams (':'), Windows reserved device names and
    segments ending in a space or dot.
    """
    check_opaque_id(value, path, max_chars)
    for segment in value.split("/"):
        if (segment in ("", ".", "..") or _UNSAFE_PATH_CHARS.search(segment)
                or len(segment) > policy.ARCHIVE_PATH_SEGMENT_MAX_CHARS
                or segment.endswith((" ", "."))
                or segment.split(".")[0].rstrip(" ").lower() in _RESERVED_NAMES):
            raise _fail(path, "must be a safe relative path using '/' separators")
    return value


def _version_field(document: dict, name: str, supported: int | tuple[int, ...]) -> None:
    if name not in document:
        raise _fail((name,), "is required")
    value = document[name]
    if not is_json_int(value):
        raise _fail((name,), "must be an integer")
    allowed = supported if isinstance(supported, tuple) else (supported,)
    if value not in allowed:
        raise _fail((name,), f"is not supported (expected {' or '.join(map(str, allowed))})",
                    ErrorCode.INCOMPATIBLE_VERSION)


def validate_manifest(raw: Any) -> PackageManifest:
    """Parse and validate `manifest.json` bytes. No filesystem or process I/O."""
    document = decode_json(raw, max_bytes=policy.MANIFEST_MAX_BYTES,
                           code=ErrorCode.PACKAGE_INVALID)
    with _boundary(ErrorCode.PACKAGE_INVALID):
        _object(document, ())
        _version_field(document, "format_version", policy.SUPPORTED_MANIFEST_FORMAT_VERSIONS)
        _version_field(document, "protocol_version", policy.PROTOCOL_VERSION)
        _fields(document, (), _MANIFEST_FIELDS, ("setup", "connections", "reconnect", "ui", "compatibility", "card", "migrations"))
        reconnect = document.get("reconnect", {"enabled": False})
        _fields(reconnect, ("reconnect",), ("enabled",))
        check_bool(reconnect["enabled"], ("reconnect", "enabled"))
        check_package_id(document["id"], ("id",))
        check_version(document["version"], ("version",))
        check_text(document["name"], ("name",))
        check_text(document["author"], ("author",))
        check_text(document["description"], ("description",), policy.DESCRIPTION_MAX_CHARS)
        if document["target"] not in policy.SUPPORTED_TARGETS:
            raise _fail(("target",), "is not a supported target")
        entrypoint = check_relative_path(document["entrypoint"], ("entrypoint",))
        if not entrypoint.lower().endswith(".exe"):
            raise _fail(("entrypoint",), "must name a .exe file")
        check_int(document["config_schema_version"], ("config_schema_version",), 1)
        config_schema = check_schema(document["config_schema"], ("config_schema",),
                                     object_root=True, closed_root=True)
        permissions = [_permission(item, ("permissions", i))
                       for i, item in enumerate(_list(document["permissions"], ("permissions",)))]
        declared = {p.id for p in permissions}
        actions = [_action(item, ("actions", i), declared)
                   for i, item in enumerate(_list(document["actions"], ("actions",)))]
        collections = [_collection(item, ("collections", i))
                       for i, item in enumerate(_list(document["collections"], ("collections",)))]
        events = [_event(item, ("events", i))
                  for i, item in enumerate(_list(document["events"], ("events",)))]
        secrets, auth, web_sessions = (_setup(document["setup"]) if "setup" in document
                                       else ([], [], []))
        connections = [_connection(item, ("connections", i)) for i, item in enumerate(
            _list(document.get("connections", []), ("connections",), policy.CONNECTIONS_MAX))]
        return PackageManifest(
            format_version=document["format_version"],
            protocol_version=document["protocol_version"],
            id=document["id"], version=document["version"], name=document["name"],
            author=document["author"], description=document["description"],
            target=document["target"], entrypoint=entrypoint,
            config_schema_version=document["config_schema_version"],
            config_schema=config_schema, permissions=permissions, actions=actions,
            collections=collections, events=events, secrets=secrets, auth=auth,
            connections=connections, web_sessions=web_sessions,
            reconnect_enabled=reconnect["enabled"], ui=document.get("ui"),
            compatibility=document.get("compatibility"), card=document.get("card"),
            migrations=document.get("migrations", ()))


def _connection(item: Any, path: Path) -> ConnectionDefinition:
    """`connections[]`: one WebSocket endpoint built from literal parts and two settings."""
    _fields(item, path, ("id", "transport", "scheme", "host_config", "port_config", "path"),
            ("subprotocols",))
    subprotocols = _list(item.get("subprotocols", []), path + ("subprotocols",),
                         policy.CONNECTION_SUBPROTOCOLS_MAX)
    fields = dict(item, subprotocols=tuple(subprotocols))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return ConnectionDefinition(**fields)


def _setup(setup: Any) -> tuple[list[SecretDefinition], list[AuthDefinition],
                                list[WebSessionDefinition]]:
    """`setup`: instance setup the host runs: secret fields, sign-ins, web sessions."""
    path = ("setup",)
    _fields(setup, path, (), ("secrets", "auth", "web_sessions"))
    items = _list(setup.get("secrets", []), path + ("secrets",), policy.SECRETS_MAX)
    secrets = [_secret(item, path + ("secrets", i)) for i, item in enumerate(items)]
    entries = _list(setup.get("auth", []), path + ("auth",), policy.AUTH_MAX)
    auth = [_auth(item, path + ("auth", i)) for i, item in enumerate(entries)]
    sessions = _list(setup.get("web_sessions", []), path + ("web_sessions",),
                     policy.WEB_SESSIONS_MAX)
    return secrets, auth, [_web_session(item, path + ("web_sessions", i))
                           for i, item in enumerate(sessions)]


def _web_session(item: Any, path: Path) -> WebSessionDefinition:
    _fields(item, path, ("id", "profile", "name"))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return WebSessionDefinition(**item)


def _auth(item: Any, path: Path) -> AuthDefinition:
    _fields(item, path, ("id", "name", "description", "required", "client_id", "scopes",
                         "token_url", "methods"), ("revocation_url", "issuer"))
    methods = _fields(item["methods"], path + ("methods",), (), ("browser", "device"))
    browser = device = None
    if "browser" in methods:
        where = path + ("methods", "browser")
        _fields(methods["browser"], where, ("authorize_url", "redirect_path"))
        with _boundary(ErrorCode.INVALID_ARGUMENT, where):
            browser = BrowserMethod(**methods["browser"])
    if "device" in methods:
        where = path + ("methods", "device")
        _fields(methods["device"], where, ("device_authorization_url", "verification_hosts"))
        hosts = _list(methods["device"]["verification_hosts"], where + ("verification_hosts",))
        with _boundary(ErrorCode.INVALID_ARGUMENT, where):
            device = DeviceMethod(methods["device"]["device_authorization_url"], tuple(hosts))
    scopes = _list(item["scopes"], path + ("scopes",), policy.AUTH_SCOPES_MAX)
    fields = {k: v for k, v in item.items() if k != "methods"}
    fields["scopes"] = tuple(scopes)
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return AuthDefinition(**fields, browser=browser, device=device)


def _secret(item: Any, path: Path) -> SecretDefinition:
    _fields(item, path, ("id", "name", "description", "required"))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return SecretDefinition(**item)


def _permission(item: Any, path: Path) -> PermissionDefinition:
    _fields(item, path, ("id", "kind", "description", "scope", "required_for_initialization"))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return PermissionDefinition(**item)


def _action(item: Any, path: Path, declared: set[str]) -> ActionDefinition:
    _fields(item, path, ("id", "name", "description", "permissions", "input_schema",
                         "output_schema", "completion"), ("dispatch", "feedback", "uses_menu", "target_collection"))
    for i, permission in enumerate(_list(item["permissions"], path + ("permissions",))):
        check_identifier(permission, path + ("permissions", i))
        if permission not in declared:
            raise _fail(path + ("permissions", i), "references an undeclared permission")
    fields = dict(item,
                  input_schema=check_schema(item["input_schema"], path + ("input_schema",),
                                            object_root=True, closed_root=True),
                  output_schema=check_schema(item["output_schema"], path + ("output_schema",)))
    if "dispatch" in item:
        dispatch = item["dispatch"]
        _fields(dispatch, path + ("dispatch",), ("mode", "key_arguments"),
                ("min_interval_ms",))
        keys = _list(dispatch["key_arguments"], path + ("dispatch", "key_arguments"))
        with _boundary(ErrorCode.INVALID_ARGUMENT, path):
            fields["dispatch"] = ActionDispatch(dispatch["mode"], tuple(keys),
                                                dispatch.get("min_interval_ms"))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return ActionDefinition(**fields)


def _collection(item: Any, path: Path) -> CollectionDefinition:
    _fields(item, path, ("id", "name", "state_schema"), ("state_ttl_seconds",))
    fields = dict(item, state_schema=check_schema(item["state_schema"], path + ("state_schema",),
                                                  object_root=True))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return CollectionDefinition(**fields)


def _event(item: Any, path: Path) -> EventDefinition:
    _fields(item, path, ("id", "payload_schema"))
    fields = dict(item, payload_schema=check_schema(item["payload_schema"],
                                                    path + ("payload_schema",), object_root=True))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return EventDefinition(**fields)


# ── Records shared by persistence and the wire ──────────────────────────────

def parse_package_pin(raw: Any, path: Path = ()) -> PackagePin:
    _fields(raw, path, ("package_id", "version", "digest"))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return PackagePin(**raw)


def parse_grant_set(raw: Any, path: Path = ()) -> GrantSet:
    _fields(raw, path, ("revision", "decisions"))
    grants = []
    for i, item in enumerate(_list(raw["decisions"], path + ("decisions",))):
        _fields(item, path + ("decisions", i), ("permission_id", "scope", "decision"))
        with _boundary(ErrorCode.INVALID_ARGUMENT, path + ("decisions", i)):
            grants.append(Grant(**item))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return GrantSet(raw["revision"], grants)


def parse_instance_record(raw: Any, path: Path = ()) -> InstanceRecord:
    """Instance record from its JSON form. The package pin may be unknown."""
    _fields(raw, path, ("instance_id", "label", "pin", "config_schema_version", "config",
                        "secret_refs", "grants", "enabled"), ("secret_locations", "update_source"))
    fields = dict(raw, pin=parse_package_pin(raw["pin"], path + ("pin",)),
                  grants=parse_grant_set(raw["grants"], path + ("grants",)),
                  secret_refs=_list(raw["secret_refs"], path + ("secret_refs",)))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return InstanceRecord(**fields)


def parse_object_record(raw: Any, path: Path = ()) -> ObjectRecord:
    _fields(raw, path, ("id", "collection_id", "label", "parent_id", "capabilities"))
    fields = dict(raw, capabilities=_list(raw["capabilities"], path + ("capabilities",)))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return ObjectRecord(**fields)


def parse_state_record(raw: Any, path: Path = ()) -> StateRecord:
    _fields(raw, path, ("object_id", "revision", "values", "available"))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return StateRecord(**raw)


def parse_object_event(raw: Any, path: Path = ()) -> ObjectEvent:
    _fields(raw, path, ("object_id", "event_id", "payload"))
    with _boundary(ErrorCode.INVALID_ARGUMENT, path):
        return ObjectEvent(**raw)


def parse_objects_changed(raw: Any, path: Path = ()) -> str:
    """`objects.changed` data: the collection whose objects were added, removed
    or renamed. An invalidation only, never a list of objects."""
    _fields(raw, path, ("collection_id",))
    return check_identifier(raw["collection_id"], path + ("collection_id",))


def parse_unavailable(raw: Any, path: Path = ()) -> tuple[ErrorCode, str, bool]:
    """`instance.unavailable` data: a worker-claimable code and safe message."""
    _fields(raw, path, ("code", "message"), ("retryable",))
    _worker_code(raw["code"], path + ("code",))
    check_text(raw["message"], path + ("message",), policy.ERROR_TEXT_MAX_CHARS)
    retryable = check_bool(raw.get("retryable", False), path + ("retryable",))
    return ErrorCode(raw["code"]), raw["message"], retryable


def _worker_code(value: Any, path: Path) -> None:
    if (not isinstance(value, str) or value not in ErrorCode.__members__
            or ErrorCode(value) in HOST_ONLY_ERROR_CODES):
        raise _fail(path, "is not an error code a worker may report")


# ── Wire messages ───────────────────────────────────────────────────────────

HOST_METHODS = frozenset({"session.initialize", "action.invoke", "discovery.list",
                          "state.snapshot", "session.shutdown", "ui.menu.event", "ui.menu.children"})
WEBSOCKET_METHODS = frozenset({"host.websocket.open", "host.websocket.send",
                               "host.websocket.receive", "host.websocket.close"})
WEB_SESSION_METHODS = frozenset({"host.web_session.info", "host.web_session.request"})
UI_METHODS = frozenset({"host.ui.toast", "host.ui.update", "host.ui.dismiss", "host.ui.asset", "host.ui.menu.open"})
WORKER_METHODS = frozenset({*UI_METHODS,"host.test.probe", "host.secret.read", "host.auth.token",
                            *WEBSOCKET_METHODS, *WEB_SESSION_METHODS})
_WEBSOCKET_HANDLE = re.compile(r"ws-[0-9a-f]{32}")
EVENT_NAMES = frozenset({"state.changed", "object.event", "objects.changed",
                         "instance.unavailable"})
SENDERS = ("host", "worker")


def _check_params(method: str, params: Any, path: Path) -> None:
    if method == "session.initialize":
        _fields(params, path, ("package_id", "package_version", "config",
                               "config_schema_version", "grants", "host_protocol_version"), ("ui_capabilities",))
        if "ui_capabilities" in params:
            caps = params["ui_capabilities"]
            _fields(caps, path+("ui_capabilities",), ("spec_versions", "size_classes", "reduced_motion"))
            if caps["spec_versions"] != [1] or type(caps["spec_versions"][0]) is not int or caps["size_classes"] != ["compact", "standard", "wide"]:
                raise _fail(path+("ui_capabilities",), "unsupported UI capabilities")
            check_bool(caps["reduced_motion"], path+("ui_capabilities", "reduced_motion"))
        check_package_id(params["package_id"], path + ("package_id",))
        check_version(params["package_version"], path + ("package_version",))
        _object(params["config"], path + ("config",))
        check_int(params["config_schema_version"], path + ("config_schema_version",), 1)
        parse_grant_set(params["grants"], path + ("grants",))
        check_int(params["host_protocol_version"], path + ("host_protocol_version",), 1)
    elif method in UI_METHODS:
        extras = {"host.ui.toast": ("spec",), "host.ui.update": ("toast_id", "patch"),
                  "host.ui.dismiss": ("toast_id",), "host.ui.asset": ("asset_id", "data"),
                  "host.ui.menu.open": ("menu_id",)}[method]
        _fields(params, path, ("permission_id", "parent_request_id", *extras))
        check_identifier(params["permission_id"], path+("permission_id",))
        if params["parent_request_id"] is not None:
            _check_prefix(params["parent_request_id"], "host", path+("parent_request_id",))
        for key in ("toast_id", "menu_id", "asset_id"):
            if key in params:
                check_identifier(params[key], path+(key,))
        if "spec" in params:
            _object(params["spec"], path+("spec",))
        if "patch" in params:
            _object(params["patch"], path+("patch",))
        if "data" in params:
            check_text(params["data"], path+("data",), 350000)
    elif method == "ui.menu.event":
        _fields(params, path, ("menu_id", "event"))
        check_identifier(params["menu_id"], path+("menu_id",))
        _object(params["event"], path+("event",))
    elif method == "ui.menu.children":
        _fields(params, path, ("menu_id", "page_id", "cursor"))
        for key in ("menu_id", "page_id"):
            check_identifier(params[key], path+(key,))
        if params["cursor"] is not None:
            check_opaque_id(params["cursor"], path+("cursor",))
    elif method == "action.invoke":
        _fields(params, path, ("action_id", "arguments", "midi"))
        check_identifier(params["action_id"], path + ("action_id",))
        _object(params["arguments"], path + ("arguments",))
        _object(params["midi"], path + ("midi",))
    elif method == "discovery.list":
        _fields(params, path, ("collection_id", "cursor"))
        check_identifier(params["collection_id"], path + ("collection_id",))
        if params["cursor"] is not None:
            check_opaque_id(params["cursor"], path + ("cursor",), policy.CURSOR_MAX_CHARS)
    elif method == "state.snapshot":
        _fields(params, path, ("object_ids",))
        ids = _list(params["object_ids"], path + ("object_ids",), policy.DISCOVERY_PAGE_MAX)
        if not ids:
            raise _fail(path + ("object_ids",), "must not be empty")
        for i, object_id in enumerate(ids):
            check_opaque_id(object_id, path + ("object_ids", i))
        check_unique(tuple(ids), path + ("object_ids",), "object ID")
    elif method == "session.shutdown":
        _fields(params, path, ())
    elif method == "host.test.probe":
        # parent_request_id: the host request this call serves (its deadline
        # caps the call), or null for a background call while Ready.
        _fields(params, path, ("permission_id", "parent_request_id"))
        check_identifier(params["permission_id"], path + ("permission_id",))
        if params["parent_request_id"] is not None:
            _check_prefix(params["parent_request_id"], "host", path + ("parent_request_id",))
    elif method == "host.secret.read":
        # One declared secret field; correlation as for host.test.probe.
        _fields(params, path, ("field_id", "parent_request_id"))
        check_identifier(params["field_id"], path + ("field_id",))
        if params["parent_request_id"] is not None:
            _check_prefix(params["parent_request_id"], "host", path + ("parent_request_id",))
    elif method == "host.auth.token":
        # The access token of one declared sign-in (C4.3).
        _fields(params, path, ("auth_id", "parent_request_id"))
        check_identifier(params["auth_id"], path + ("auth_id",))
        if params["parent_request_id"] is not None:
            _check_prefix(params["parent_request_id"], "host", path + ("parent_request_id",))
    elif method in WEBSOCKET_METHODS:
        # C5 (CONNECTIONS.md): open names a declared connection; the others a handle.
        if method == "host.websocket.open":
            _fields(params, path, ("connection_id", "parent_request_id"))
            check_identifier(params["connection_id"], path + ("connection_id",))
        else:
            extra = ("text",) if method == "host.websocket.send" else ()
            _fields(params, path, ("handle", "parent_request_id") + extra)
            check_websocket_handle(params["handle"], path + ("handle",))
            if extra:
                check_websocket_text(params["text"], path + ("text",))
        if params["parent_request_id"] is not None:
            _check_prefix(params["parent_request_id"], "host", path + ("parent_request_id",))
    elif method == "host.web_session.info":
        # Status of one declared web session; correlation as for host.test.probe.
        _fields(params, path, ("session_id", "parent_request_id"))
        check_identifier(params["session_id"], path + ("session_id",))
        if params["parent_request_id"] is not None:
            _check_prefix(params["parent_request_id"], "host", path + ("parent_request_id",))
    elif method == "host.web_session.request":
        # One fixed operation of a web session; `body` is the opaque request
        # body the host signs and forwards (its shape is the profile's, bounded
        # by the envelope). The host maps `operation` to a path and never lets
        # the worker choose a path or add headers.
        _fields(params, path, ("session_id", "operation", "body", "parent_request_id"))
        check_identifier(params["session_id"], path + ("session_id",))
        check_identifier(params["operation"], path + ("operation",))
        if params["parent_request_id"] is not None:
            _check_prefix(params["parent_request_id"], "host", path + ("parent_request_id",))


def check_websocket_handle(value: Any, path: Path) -> str:
    """An opaque connection handle issued by the host (`ws-` and 32 hex digits)."""
    if not isinstance(value, str) or not _WEBSOCKET_HANDLE.fullmatch(value):
        raise _fail(path, "must be a connection handle")
    return value


def check_websocket_text(value: Any, path: Path) -> str:
    """One complete text message: valid UTF-8 of at most WEBSOCKET_MESSAGE_MAX_BYTES."""
    if not isinstance(value, str):
        raise _fail(path, "must be text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise _fail(path, "must be valid Unicode text") from None
    if size > policy.WEBSOCKET_MESSAGE_MAX_BYTES:
        raise _fail(path, f"must be at most {policy.WEBSOCKET_MESSAGE_MAX_BYTES} UTF-8 bytes")
    return value


def _check_event_data(name: str, data: Any, path: Path) -> None:
    if name == "state.changed":
        parse_state_record(data, path)
    elif name == "object.event":
        parse_object_event(data, path)
    elif name == "objects.changed":
        parse_objects_changed(data, path)
    else:
        parse_unavailable(data, path)


def decode_message(frame: Any, *, sender: str) -> Message:
    """Wire-validate one frame (without its line terminator) from `sender`.

    `sender` is "host" or "worker". Request IDs must use the sender's prefix,
    responses must answer the other side's prefix, only workers emit events,
    and workers cannot report host-only error codes. Every failure is a
    PROTOCOL_ERROR; the caller decides how to tear down the session.
    """
    if sender not in SENDERS:
        raise ValueError("sender must be 'host' or 'worker'")
    code = ErrorCode.PROTOCOL_ERROR
    if isinstance(frame, (bytes, bytearray)) and (b"\n" in frame or b"\r" in frame):
        raise _fail((), "must be a single line without its terminator", code)
    document = decode_json(frame, max_bytes=policy.MESSAGE_MAX_BYTES - 1, code=code)
    return _parse_document(document, sender)


def _parse_document(document: Any, sender: str) -> Message:
    """Envelope, method and event validation shared by decoding and encoding."""
    code = ErrorCode.PROTOCOL_ERROR
    peer = "worker" if sender == "host" else "host"
    with _boundary(code):
        _object(document, ())
        if "v" not in document:
            raise _fail(("v",), "is required")
        if not is_json_int(document["v"]) or document["v"] != policy.PROTOCOL_VERSION:
            raise _fail(("v",), f"is not a supported protocol version "
                                f"(expected {policy.PROTOCOL_VERSION})")
        kind = document.get("kind")
        if kind == "request":
            _fields(document, (), ("v", "kind", "id", "method", "timeout_ms", "params"))
            methods = HOST_METHODS if sender == "host" else WORKER_METHODS
            check_identifier(document["method"], ("method",))
            if document["method"] not in methods:
                raise _fail(("method",), "is not a supported method for this sender")
            limit = (policy.REQUEST_TIMEOUT_MAX_MS if sender == "host"
                     else policy.HOST_SERVICE_TIMEOUT_MAX_S * 1000)
            check_int(document["timeout_ms"], ("timeout_ms",), 1, limit)
            _check_prefix(document["id"], sender)
            _check_params(document["method"], document["params"], ("params",))
            return Request(document["id"], document["method"], document["timeout_ms"],
                           document["params"])
        if kind == "response":
            if "ok" not in document:
                raise _fail(("ok",), "is required")
            ok = check_bool(document["ok"], ("ok",))
            _fields(document, (), ("v", "kind", "id", "ok", "result" if ok else "error"))
            _check_prefix(document["id"], peer)
            if ok:
                return Response(document["id"], True, result=document["result"])
            error = _fields(document["error"], ("error",),
                            ("code", "message", "outcome_unknown"), ("retryable",))
            if sender == "worker":
                _worker_code(error["code"], ("error", "code"))
            with _boundary(code, ("error",)):
                info = ErrorInfo(**error)
            return Response(document["id"], False, error=info)
        if kind == "event":
            if sender != "worker":
                raise _fail(("kind",), "is only sent by workers")
            _fields(document, (), ("v", "kind", "name", "seq", "data"))
            check_identifier(document["name"], ("name",))
            if document["name"] not in EVENT_NAMES:
                raise _fail(("name",), "is not a supported event")
            check_int(document["seq"], ("seq",), 1)
            _check_event_data(document["name"], document["data"], ("data",))
            return Event(document["name"], document["seq"], document["data"])
        raise _fail(("kind",), "must be 'request', 'response' or 'event'")


def _check_prefix(request_id: Any, origin: str, path: Path = ("id",)) -> None:
    check_opaque_id(request_id, path)
    if not request_id.startswith(REQUEST_ID_PREFIXES[origin]):
        raise _fail(path, f"must be a {origin}-originated ID "
                          f"('{REQUEST_ID_PREFIXES[origin]}' prefix)")
    check_request_id(request_id, path)


def _sender_of(message: Message) -> str:
    """Requests come from their ID's originator, responses from the other side."""
    if isinstance(message, Event):
        return "worker"
    host_id = message.id.startswith(REQUEST_ID_PREFIXES["host"])
    if isinstance(message, Request):
        return "host" if host_id else "worker"
    return "worker" if host_id else "host"


def encode_message(message: Message) -> bytes:
    """Compact UTF-8 LF-terminated frame.

    Runs the decoder's envelope, method-parameter and event validation for the
    message's sender, so nothing is encoded that the peer would reject.
    """
    if not isinstance(message, (Request, Response, Event)):
        raise TypeError("encode_message expects a Request, Response or Event")
    with _boundary(ErrorCode.PROTOCOL_ERROR):
        wire = copy_json(message.to_wire())
    _parse_document(wire, _sender_of(message))
    frame = json.dumps(wire, ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False).encode("utf-8") + b"\n"
    if len(frame) > policy.MESSAGE_MAX_BYTES:
        raise _fail((), f"exceeds the maximum size of {policy.MESSAGE_MAX_BYTES} bytes",
                    ErrorCode.PROTOCOL_ERROR)
    return frame


def parse_method_result(method: str, result: Any) -> Any:
    """Wire-validate a success result for a non-action method.

    Returns a validated dict for `session.initialize`/`host.test.probe`/
    `host.secret.read`/`host.auth.token`, a
    `DiscoveryPage`, a tuple of `StateRecord`, or an empty dict for shutdown.
    `action.invoke` results are validated per action by `validate_result`.
    """
    code = ErrorCode.PROTOCOL_ERROR
    path: Path = ("result",)
    with _boundary(code):
        if method in UI_METHODS:
            _fields(result, path, ("accepted",))
            check_bool(result["accepted"], path+("accepted",))
            return copy_json(result)
        if method == "ui.menu.event":
            _fields(result, path, ())
            return {}
        if method == "ui.menu.children":
            _fields(result, path, ("spec", "next_cursor"))
            from .overlay_spec import validate_overlay_spec
            spec = validate_overlay_spec(result["spec"])
            if spec.get("surface") != "menu":
                raise _fail(path, "menu surface required")
            if result["next_cursor"] is not None:
                check_opaque_id(result["next_cursor"], path+("next_cursor",))
            return {"spec": spec, "next_cursor": result["next_cursor"]}
        if method == "session.initialize":
            _fields(result, path, ("package_id", "package_version", "protocol_version"))
            check_package_id(result["package_id"], path + ("package_id",))
            check_version(result["package_version"], path + ("package_version",))
            check_int(result["protocol_version"], path + ("protocol_version",), 1)
            return copy_json(result)
        if method == "discovery.list":
            _fields(result, path, ("objects", "next_cursor"))
            objects = _list(result["objects"], path + ("objects",), policy.DISCOVERY_PAGE_MAX)
            records = [parse_object_record(o, path + ("objects", i)) for i, o in enumerate(objects)]
            with _boundary(code, path):
                return DiscoveryPage(records, result["next_cursor"])
        if method == "state.snapshot":
            _fields(result, path, ("states",))
            states = _list(result["states"], path + ("states",), policy.DISCOVERY_PAGE_MAX)
            records = tuple(parse_state_record(s, path + ("states", i))
                            for i, s in enumerate(states))
            check_unique(tuple(r.object_id for r in records), path + ("states",), "object ID")
            return records
        if method == "session.shutdown":
            _fields(result, path, ())
            return {}
        if method == "host.test.probe":
            _fields(result, path, ("allowed",))
            check_bool(result["allowed"], path + ("allowed",))
            return copy_json(result)
        if method == "host.secret.read":
            _fields(result, path, ("value",))
            check_secret_value(result["value"], path + ("value",))
            return {"value": result["value"]}
        if method == "host.auth.token":
            _fields(result, path, ("access_token", "token_type", "expires_at", "scopes"))
            check_secret_value_text(result["access_token"], path + ("access_token",),
                                    policy.AUTH_ACCESS_TOKEN_MAX_BYTES)
            if result["token_type"] != "Bearer":
                raise _fail(path + ("token_type",), "must be Bearer")
            if result["expires_at"] is not None:
                check_int(result["expires_at"], path + ("expires_at",), 0)
            scopes = _list(result["scopes"], path + ("scopes",), policy.AUTH_SCOPES_MAX)
            for i, scope in enumerate(scopes):
                check_scope_token(scope, path + ("scopes", i))
            return {"access_token": result["access_token"], "token_type": "Bearer",
                    "expires_at": result["expires_at"], "scopes": list(scopes)}
        if method == "host.websocket.open":
            _fields(result, path, ("handle",))
            check_websocket_handle(result["handle"], path + ("handle",))
            return {"handle": result["handle"]}
        if method in ("host.websocket.send", "host.websocket.close"):
            _fields(result, path, ())
            return {}
        if method == "host.websocket.receive":
            kind = result.get("kind") if isinstance(result, dict) else None
            if kind == "message":
                _fields(result, path, ("kind", "text"))
                return {"kind": "message",
                        "text": check_websocket_text(result["text"], path + ("text",))}
            if kind == "wait":
                _fields(result, path, ("kind",))
                return {"kind": "wait"}
            if kind == "closed":
                _fields(result, path, ("kind", "code"))
                return {"kind": "closed",
                        "code": check_int(result["code"], path + ("code",), 1000, 4999)}
            raise _fail(path + ("kind",), "must be message, wait or closed")
        if method == "host.web_session.info":
            _fields(result, path, ("connected", "account", "structure_id",
                                   "structure_name", "epoch"))
            check_bool(result["connected"], path + ("connected",))
            if result["account"] is not None:
                check_text(result["account"], path + ("account",))
            if result["structure_id"] is not None:
                check_opaque_id(result["structure_id"], path + ("structure_id",))
            if result["structure_name"] is not None:
                check_text(result["structure_name"], path + ("structure_name",))
            check_int(result["epoch"], path + ("epoch",), 0)
            return copy_json(result)
        if method == "host.web_session.request":
            _fields(result, path, ("status", "data"))
            check_int(result["status"], path + ("status",), 100, 599)
            return {"status": result["status"], "data": copy_json(result["data"])}
    raise ValueError(f"no wire result contract for method {method!r}")


# ── Domain validation ───────────────────────────────────────────────────────

def resolve_action(manifest: PackageManifest, action_id: Any) -> ActionDefinition:
    """The declared action, or INVALID_ARGUMENT at `/action_id`."""
    with _boundary(ErrorCode.INVALID_ARGUMENT, ("action_id",)):
        check_identifier(action_id, ())
        return manifest.action(action_id)


def validate_invocation(manifest: PackageManifest, request: Request) -> dict:
    """Domain check of an `action.invoke` request; returns validated arguments."""
    if request.method != "action.invoke":
        raise ValueError("not an action.invoke request")
    params = request.params
    try:
        action = resolve_action(manifest, params["action_id"])
    except DomainError as error:
        raise error.at(("params",)) from None
    try:
        return validate_arguments(action, thaw_json(params["arguments"]))
    except DomainError as error:
        raise error.at(("params", "arguments")) from None


def validate_initialize_result(pin: PackagePin, result: Any) -> dict:
    """Worker's initialization echo must match the pin and supported protocol.

    The digest is verified by the host from installed files, never from a
    worker claim, so it is not part of this check.
    """
    checked = parse_method_result("session.initialize", result)
    if checked["package_id"] != pin.package_id:
        raise _fail(("result", "package_id"), "does not match the pinned package",
                    ErrorCode.PROTOCOL_ERROR)
    if checked["package_version"] != pin.version:
        raise _fail(("result", "package_version"), "does not match the pinned version",
                    ErrorCode.PROTOCOL_ERROR)
    if checked["protocol_version"] != policy.PROTOCOL_VERSION:
        raise _fail(("result", "protocol_version"), "is not a supported protocol version",
                    ErrorCode.INCOMPATIBLE_VERSION)
    return checked


def validate_discovery_page(manifest: PackageManifest, collection_id: str,
                            page: DiscoveryPage) -> DiscoveryPage:
    """Objects belong to the requested declared collection and declared actions."""
    code = ErrorCode.PROTOCOL_ERROR
    with _boundary(ErrorCode.INVALID_ARGUMENT, ("collection_id",)):
        manifest.collection(collection_id)
    actions = {a.id for a in manifest.actions}
    for i, obj in enumerate(page.objects):
        where = ("result", "objects", i)
        if obj.collection_id != collection_id:
            raise _fail(where + ("collection_id",), "does not match the requested collection",
                        code)
        for j, action_id in enumerate(obj.capabilities):
            if action_id not in actions:
                raise _fail(where + ("capabilities", j), "references an undeclared action", code)
    return page


def validate_state_values(collection: CollectionDefinition, record: StateRecord) -> dict:
    """Validated copy of a state record's values against its collection schema."""
    try:
        return validate_value(collection.state_schema, thaw_json(record.values),
                              code=ErrorCode.PROTOCOL_ERROR)
    except DomainError as error:
        raise error.at(("values",)) from None


def validate_objects_changed(manifest: PackageManifest, collection_id: str) -> str:
    """An `objects.changed` invalidation names a declared collection."""
    with _boundary(ErrorCode.PROTOCOL_ERROR, ("collection_id",)):
        manifest.collection(collection_id)
    return collection_id


def validate_object_event(manifest: PackageManifest, event: ObjectEvent) -> dict:
    """Validated copy of an occurrence payload for a declared event."""
    with _boundary(ErrorCode.PROTOCOL_ERROR, ("event_id",)):
        definition = manifest.event(event.event_id)
    try:
        return validate_value(definition.payload_schema, thaw_json(event.payload),
                              code=ErrorCode.PROTOCOL_ERROR)
    except DomainError as error:
        raise error.at(("payload",)) from None


def validate_grants(manifest: PackageManifest, grants: GrantSet) -> GrantSet:
    """Every decision names a declared permission with its declared scope.

    A `network.websocket` grant instead stores the approved endpoint with
    its connection (CONNECTIONS.md, "Declaration and consent"); a denied one
    may keep just the declared scope. Whether that endpoint is the one the
    current settings select is decided at use (`permissions.is_granted`).
    """
    for i, grant in enumerate(grants.decisions):
        where = ("decisions", i)
        try:
            permission = manifest.permission(grant.permission_id)
        except DomainError:
            raise _fail(where + ("permission_id",), "references an undeclared permission") from None
        scope = thaw_json(grant.scope)
        if scope == thaw_json(permission.scope):
            if permission.kind == "network.websocket" and grant.decision is GrantDecision.GRANTED:
                raise _fail(where + ("scope",), "must name the approved endpoint")
            continue
        if permission.kind != "network.websocket" or set(scope) != {"connection", "endpoint"} \
                or scope["connection"] != permission.scope["connection"]:
            raise _fail(where + ("scope",), "does not match the declared scope")
        check_endpoint(manifest.connection(scope["connection"]), scope["endpoint"],
                       where + ("scope", "endpoint"))
    return grants


def check_admission(state: LifecycleState) -> None:
    """Only Ready instances accept new actions."""
    state = LifecycleState(state)
    if state is LifecycleState.READY:
        return
    if state is LifecycleState.NEEDS_SETUP:
        raise DomainError(ErrorCode.NEEDS_SETUP, (), "the instance needs setup")
    raise DomainError(ErrorCode.INSTANCE_UNAVAILABLE, (), "the instance is not ready")
