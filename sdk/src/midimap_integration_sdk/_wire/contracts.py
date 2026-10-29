"""Immutable integration value models and the domain error vocabulary.

Every model checks its own invariants on construction and owns private frozen
copies of any JSON it receives: dictionaries become read-only mapping proxies and
arrays become tuples, so a caller can neither mutate a model nor observe later
mutation through it. `thaw_json` returns a fresh, ordinary JSON copy.

Models raise `DomainError` with a field path relative to the model. Parsers in
`validation` prefix that path and choose the error code for their boundary.
Nothing here performs I/O or imports optional dependencies.
"""
from __future__ import annotations

import ipaddress
import math
import re
import uuid
from collections.abc import Mapping
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from . import policy


# ── Errors ──────────────────────────────────────────────────────────────────

class ErrorCode(StrEnum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NEEDS_SETUP = "NEEDS_SETUP"
    INSTANCE_UNAVAILABLE = "INSTANCE_UNAVAILABLE"
    TARGET_MISSING = "TARGET_MISSING"
    SATURATED = "SATURATED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    WORKER_EXITED = "WORKER_EXITED"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    # Host repository/store codes; a worker may never claim these.
    PACKAGE_CONFLICT = "PACKAGE_CONFLICT"
    PACKAGE_INVALID = "PACKAGE_INVALID"
    PACKAGE_MISSING = "PACKAGE_MISSING"
    STORE_ERROR = "STORE_ERROR"
    INCOMPATIBLE_VERSION = "INCOMPATIBLE_VERSION"


HOST_ONLY_ERROR_CODES = frozenset({
    ErrorCode.PACKAGE_CONFLICT, ErrorCode.PACKAGE_INVALID, ErrorCode.PACKAGE_MISSING,
    ErrorCode.STORE_ERROR, ErrorCode.INCOMPATIBLE_VERSION,
})

Path = tuple[str | int, ...]


def json_pointer(path: Path) -> str:
    """RFC 6901 pointer for a field path; the root is the empty string."""
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)


class DomainError(Exception):
    """A validation or domain failure with a field path and safe description.

    `message` is written by the host and never contains the rejected value, so
    the error can be logged or shown without disclosing caller input.
    """

    def __init__(self, code: ErrorCode | str, path: Path = (), message: str = "is invalid",
                 *, retryable: bool = False, outcome_unknown: bool = False):
        self.code = ErrorCode(code)
        self.path = tuple(path)
        self.message = message[:policy.ERROR_TEXT_MAX_CHARS]
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        super().__init__(f"{self.code.value} at {self.pointer or '<root>'}: {self.message}")

    @property
    def pointer(self) -> str:
        return json_pointer(self.path)

    def at(self, prefix: Path, code: ErrorCode | None = None) -> DomainError:
        """Copy of this error relocated below `prefix`, optionally recoded."""
        return DomainError(code or self.code, tuple(prefix) + self.path, self.message,
                           retryable=self.retryable, outcome_unknown=self.outcome_unknown)

    def info(self) -> ErrorInfo:
        text = f"{self.pointer}: {self.message}" if self.path else self.message
        return ErrorInfo(self.code, text[:policy.ERROR_TEXT_MAX_CHARS],
                         self.retryable, self.outcome_unknown)


def _fail(path: Path, message: str) -> DomainError:
    return DomainError(ErrorCode.INVALID_ARGUMENT, path, message)


# ── Primitive field checks (shared by models and parsers) ───────────────────

_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")
_PACKAGE_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_PACKAGE_ID = re.compile(rf"{_PACKAGE_SEGMENT}(?:\.{_PACKAGE_SEGMENT})+")
_VERSION = re.compile(r"(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})\.(?:0|[1-9][0-9]{0,8})")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def is_json_int(value: Any) -> bool:
    """True for a JSON integer; Python's bool subclass is not an integer."""
    return isinstance(value, int) and not isinstance(value, bool)


def check_int(value: Any, path: Path, minimum: int | None = None,
              maximum: int | None = None) -> int:
    if not is_json_int(value):
        raise _fail(path, "must be an integer")
    if minimum is not None and value < minimum:
        raise _fail(path, f"must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise _fail(path, f"must be at most {maximum}")
    return value


def check_bool(value: Any, path: Path) -> bool:
    if not isinstance(value, bool):
        raise _fail(path, "must be a boolean")
    return value


def _check_str(value: Any, path: Path, max_chars: int) -> str:
    if not isinstance(value, str):
        raise _fail(path, "must be a string")
    if not value:
        raise _fail(path, "must not be empty")
    if len(value) > max_chars:
        raise _fail(path, f"must be at most {max_chars} characters")
    return value


def check_text(value: Any, path: Path, max_chars: int = policy.DISPLAY_NAME_MAX_CHARS) -> str:
    """Nonblank human-readable text without control characters."""
    _check_str(value, path, max_chars)
    if _CONTROL.search(value) or not value.strip():
        raise _fail(path, "must be visible text without control characters")
    return value


def check_opaque_id(value: Any, path: Path, max_chars: int = policy.IDENTIFIER_MAX_CHARS) -> str:
    """Provider-chosen identifier: any printable string, compared exactly."""
    _check_str(value, path, max_chars)
    if _CONTROL.search(value):
        raise _fail(path, "must not contain control characters")
    return value


def check_identifier(value: Any, path: Path) -> str:
    """Stable author identifier such as `set_level` or `simulator.probe`."""
    _check_str(value, path, policy.IDENTIFIER_MAX_CHARS)
    if not _IDENTIFIER.fullmatch(value):
        raise _fail(path, "must be a lowercase identifier (letters, digits, '.', '_', '-')")
    return value


def check_package_id(value: Any, path: Path) -> str:
    _check_str(value, path, policy.IDENTIFIER_MAX_CHARS)
    if not _PACKAGE_ID.fullmatch(value):
        raise _fail(path, "must be a lowercase reverse-domain package ID")
    return value


def check_version(value: Any, path: Path) -> str:
    _check_str(value, path, policy.IDENTIFIER_MAX_CHARS)
    if not _VERSION.fullmatch(value):
        raise _fail(path, "must be a numeric MAJOR.MINOR.PATCH version")
    return value


def check_digest(value: Any, path: Path) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise _fail(path, "must be a lowercase hexadecimal SHA-256 digest")
    return value


def check_uuid(value: Any, path: Path) -> str:
    """Canonical lowercase hyphenated UUID text."""
    try:
        canonical = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        canonical = None
    if canonical is None or canonical != value:
        raise _fail(path, "must be a canonical lowercase UUID")
    return value


def check_unique(values: tuple, path: Path, what: str = "value") -> None:
    seen = set()
    for index, value in enumerate(values):
        if value in seen:
            raise _fail(path + (index,), f"duplicates an earlier {what}")
        seen.add(value)


# ── JSON ownership ──────────────────────────────────────────────────────────

def copy_json(value: Any, path: Path = (), depth_limit: int = policy.JSON_DEPTH_MAX) -> Any:
    """Strict private copy of a JSON value supplied as Python objects.

    Accepts only dict (string keys), list, str, int, finite float, bool and None.
    Tuples, other mappings, NaN/infinity, unencodable strings and nesting beyond
    `depth_limit` containers are rejected; nothing is coerced.
    """
    def walk(item: Any, where: Path, depth: int) -> Any:
        if isinstance(item, (dict, list)):
            if depth >= depth_limit:
                raise _fail(where, f"exceeds the maximum nesting depth of {depth_limit}")
            if isinstance(item, list):
                return [walk(v, where + (i,), depth + 1) for i, v in enumerate(item)]
            out = {}
            for key, v in item.items():
                if not isinstance(key, str):
                    raise _fail(where, "has a non-string field name")
                _check_encodable(key, where)
                out[key] = walk(v, where + (key,), depth + 1)
            return out
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise _fail(where, "must be a finite number")
            return item
        if isinstance(item, str):
            _check_encodable(item, where)
            return item
        raise _fail(where, "is not a JSON value")
    return walk(value, tuple(path), 0)


def _check_encodable(text: str, path: Path) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _fail(path, "contains invalid Unicode") from None


def freeze_json(value: Any) -> Any:
    """Read-only structure over an already validated private JSON copy."""
    if isinstance(value, dict):
        return MappingProxyType({k: freeze_json(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(v) for v in value)
    return value


def thaw_json(value: Any) -> Any:
    """Fresh ordinary dict/list JSON copy of a frozen (or plain) value."""
    if isinstance(value, Mapping):
        return {k: thaw_json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json(v) for v in value]
    return value


def own_object(value: Any, path: Path) -> MappingProxyType:
    """Private frozen copy of a JSON object (frozen inputs are accepted)."""
    copied = copy_json(thaw_json(value), path)
    if not isinstance(copied, dict):
        raise _fail(path, "must be an object")
    return freeze_json(copied)


def _set(obj: Any, name: str, value: Any) -> None:
    object.__setattr__(obj, name, value)


# ── Package definitions ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PackagePin:
    """Exact installed package identity: ID, version and archive SHA-256."""
    package_id: str
    version: str
    digest: str

    def __post_init__(self):
        check_package_id(self.package_id, ("package_id",))
        check_version(self.version, ("version",))
        check_digest(self.digest, ("digest",))

    def to_json(self) -> dict:
        return {"package_id": self.package_id, "version": self.version, "digest": self.digest}


PERMISSION_KINDS = frozenset({"test.probe", "secret.read", "auth.token",  # each with a host service
                              "network.websocket", "network.web_session", "ui.overlay"})


@dataclass(frozen=True)
class PermissionDefinition:
    id: str
    kind: str
    description: str
    scope: Mapping[str, Any]
    required_for_initialization: bool

    def __post_init__(self):
        check_identifier(self.id, ("id",))
        if not isinstance(self.kind, str) or self.kind not in PERMISSION_KINDS:
            raise _fail(("kind",), "is not a supported permission kind")
        check_text(self.description, ("description",), policy.DESCRIPTION_MAX_CHARS)
        _set(self, "scope", check_scope(self.kind, self.scope, ("scope",)))
        check_bool(self.required_for_initialization, ("required_for_initialization",))


def check_scope(kind: str, scope: Any, path: Path) -> MappingProxyType:
    """Owned copy of a scope whose exact shape is defined by its permission kind."""
    owned = own_object(scope, path)
    if kind == "test.probe":
        if set(owned) != {"resource"}:
            raise _fail(path, "must contain exactly the field 'resource'")
        check_identifier(owned["resource"], path + ("resource",))
    elif kind == "secret.read":
        if set(owned) != {"field"}:
            raise _fail(path, "must contain exactly the field 'field'")
        check_identifier(owned["field"], path + ("field",))
    elif kind == "auth.token":
        if set(owned) != {"auth"}:
            raise _fail(path, "must contain exactly the field 'auth'")
        check_identifier(owned["auth"], path + ("auth",))
    elif kind == "network.websocket":
        if set(owned) != {"connection"}:
            raise _fail(path, "must contain exactly the field 'connection'")
        check_identifier(owned["connection"], path + ("connection",))
    elif kind == "network.web_session":
        if set(owned) != {"session"}:
            raise _fail(path, "must contain exactly the field 'session'")
        check_identifier(owned["session"], path + ("session",))
    elif kind == "ui.overlay":
        if set(owned) != {"surface"} or owned["surface"] not in ("toast", "menu"):
            raise _fail(path, "must contain exactly surface: toast or menu")
    return owned


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SCOPE_TOKEN = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+")          # RFC 6749 scope-token
_REDIRECT_PATH = re.compile(r"/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?")
_ACCESS_TOKEN = re.compile(r"[\x21-\x7e]+")                          # printable, no space


def check_dns_host(value: Any, path: Path) -> str:
    """A DNS host in lowercase ASCII (IDNA) form; never an IP literal or localhost."""
    _check_str(value, path, 253)
    if not value.isascii() or value != value.lower():
        raise _fail(path, "must be a lowercase ASCII (IDNA) host name")
    labels = value.split(".")
    if len(labels) < 2 or not all(_DNS_LABEL.fullmatch(label) for label in labels):
        raise _fail(path, "must be a DNS host name")
    if labels[-1].isdigit():
        raise _fail(path, "must not be an IP address")
    if value == "localhost" or value.endswith(".localhost"):
        raise _fail(path, "must not be localhost")
    return value


def check_https_url(value: Any, path: Path) -> str:
    """An absolute https URL on a DNS host, without user info, query or fragment."""
    _check_str(value, path, policy.AUTH_URL_MAX_CHARS)
    if _CONTROL.search(value) or any(c.isspace() for c in value) or not value.isascii():
        raise _fail(path, "must be a URL without spaces or control characters")
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise _fail(path, "must be an https URL")
    if "?" in value or "#" in value:
        raise _fail(path, "must have no query or fragment")
    netloc = parts.netloc
    if not netloc or "@" in netloc:
        raise _fail(path, "must name a host without user information")
    host, sep, port = netloc.rpartition(":") if ":" in netloc else (netloc, "", "")
    if sep and not (port.isdigit() and 0 < int(port) <= 65535):
        raise _fail(path, "has an invalid port")
    check_dns_host(host, path)
    return value


def url_origin(url: str) -> str:
    """Normalized `https://host:port` of a validated URL (the default port made explicit)."""
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port or 443
    return f"https://{host}:{port}"


def check_secret_value_text(value: Any, path: Path, max_bytes: int) -> str:
    """Printable ASCII token text (no spaces) of at most `max_bytes`; never echoed."""
    if not isinstance(value, str) or not value or len(value) > max_bytes \
            or not _ACCESS_TOKEN.fullmatch(value):
        raise _fail(path, f"must be printable token text of at most {max_bytes} bytes")
    return value


def check_scope_token(value: Any, path: Path) -> str:
    _check_str(value, path, 128)
    if not _SCOPE_TOKEN.fullmatch(value):
        raise _fail(path, "must be an OAuth scope token")
    return value


@dataclass(frozen=True)
class BrowserMethod:
    """Browser sign-in with a 127.0.0.1 loopback redirect (C4.3)."""
    authorize_url: str
    redirect_path: str

    def __post_init__(self):
        check_https_url(self.authorize_url, ("authorize_url",))
        _check_str(self.redirect_path, ("redirect_path",), 128)
        if not _REDIRECT_PATH.fullmatch(self.redirect_path) or "/../" in self.redirect_path + "/" \
                or "/./" in self.redirect_path + "/":
            raise _fail(("redirect_path",), "must be a plain absolute path such as /callback")


@dataclass(frozen=True)
class DeviceMethod:
    """Device-code sign-in (RFC 8628)."""
    device_authorization_url: str
    verification_hosts: tuple[str, ...]

    def __post_init__(self):
        check_https_url(self.device_authorization_url, ("device_authorization_url",))
        hosts = tuple(self.verification_hosts)
        _set(self, "verification_hosts", hosts)
        if not 1 <= len(hosts) <= policy.AUTH_VERIFICATION_HOSTS_MAX:
            raise _fail(("verification_hosts",),
                        f"must list 1 to {policy.AUTH_VERIFICATION_HOSTS_MAX} hosts")
        for i, host in enumerate(hosts):
            check_dns_host(host, ("verification_hosts", i))
        check_unique(hosts, ("verification_hosts",), "host")


@dataclass(frozen=True)
class AuthDefinition:
    """One sign-in an instance's setup asks for (`setup.auth[]`, C4.3).

    Public clients only: there is no client secret. Tokens live only in the
    host's secret store; a worker may read the access token through its one
    `auth.token` permission.
    """
    id: str
    name: str
    description: str
    required: bool
    client_id: str
    scopes: tuple[str, ...]
    token_url: str
    revocation_url: str | None = None
    issuer: str | None = None
    browser: BrowserMethod | None = None
    device: DeviceMethod | None = None

    def __post_init__(self):
        check_identifier(self.id, ("id",))
        if self.id.startswith(policy.SECRET_RESERVED_PREFIX):
            raise _fail(("id",), f"must not start with the reserved prefix "
                                 f"'{policy.SECRET_RESERVED_PREFIX}'")
        check_text(self.name, ("name",))
        check_text(self.description, ("description",), policy.DESCRIPTION_MAX_CHARS)
        check_bool(self.required, ("required",))
        _check_str(self.client_id, ("client_id",), policy.AUTH_CLIENT_ID_MAX_CHARS)
        if _CONTROL.search(self.client_id) or not self.client_id.isprintable():
            raise _fail(("client_id",), "must be printable text")
        scopes = tuple(self.scopes)
        _set(self, "scopes", scopes)
        if len(scopes) > policy.AUTH_SCOPES_MAX:
            raise _fail(("scopes",), f"must contain at most {policy.AUTH_SCOPES_MAX} items")
        for i, scope in enumerate(scopes):
            check_scope_token(scope, ("scopes", i))
        check_unique(scopes, ("scopes",), "scope")
        check_https_url(self.token_url, ("token_url",))
        for name in ("revocation_url", "issuer"):
            if getattr(self, name) is not None:
                check_https_url(getattr(self, name), (name,))
        if self.browser is None and self.device is None:
            raise _fail(("methods",), "must declare a browser or device method")
        for name, kind in (("browser", BrowserMethod), ("device", DeviceMethod)):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), kind):
                raise _fail(("methods", name), "is not a sign-in method")

    @property
    def coordination_key(self) -> tuple[str, str]:
        """Service-wide revocation key: exact client ID and token-endpoint origin."""
        return self.client_id, url_origin(self.token_url)


def check_secret_value(value: Any, path: Path) -> str:
    """Secret text as it crosses the wire: nonempty UTF-8 without NUL, at most
    SECRET_VALUE_MAX_BYTES bytes. Errors never contain the value."""
    if not isinstance(value, str):
        raise _fail(path, "must be a string")
    if not value or "\x00" in value:
        raise _fail(path, "must be nonempty text without NUL characters")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise _fail(path, "contains invalid Unicode") from None
    if size > policy.SECRET_VALUE_MAX_BYTES:
        raise _fail(path, f"must be at most {policy.SECRET_VALUE_MAX_BYTES} UTF-8 bytes")
    return value


@dataclass(frozen=True)
class SecretDefinition:
    """One secret field an instance's setup asks for (`setup.secrets[]`).

    Values never appear in the manifest, configuration or records; the host
    keeps them in its secret store, addressed by instance and this ID.
    """
    id: str
    name: str
    description: str
    required: bool

    def __post_init__(self):
        check_identifier(self.id, ("id",))
        if self.id.startswith(policy.SECRET_RESERVED_PREFIX):
            raise _fail(("id",), f"must not start with the reserved prefix "
                                 f"'{policy.SECRET_RESERVED_PREFIX}'")
        check_text(self.name, ("name",))
        check_text(self.description, ("description",), policy.DESCRIPTION_MAX_CHARS)
        check_bool(self.required, ("required",))


@dataclass(frozen=True)
class WebSessionDefinition:
    """One host-run browser web session an instance's setup asks for
    (`setup.web_sessions[]`, manifest format 2).

    The host owns the whole session: an isolated browser profile, the captured
    cookies, authentication signing and HTTPS execution. The package names a
    host-implemented `profile` from a fixed allowlist; it supplies no browser
    JavaScript, origins or RPC paths. A worker reaches the session only through
    its one `network.web_session` permission and the `host.web_session.*`
    services, which run a fixed set of operations the profile maps to paths.
    """
    id: str
    profile: str
    name: str

    def __post_init__(self):
        check_identifier(self.id, ("id",))
        if self.id.startswith(policy.SECRET_RESERVED_PREFIX):
            raise _fail(("id",), f"must not start with the reserved prefix "
                                 f"'{policy.SECRET_RESERVED_PREFIX}'")
        if self.profile not in policy.WEB_SESSION_PROFILES:
            raise _fail(("profile",), "is not a host-implemented web-session profile")
        check_text(self.name, ("name",))


# ── Connections (C5, CONNECTIONS.md) ────────────────────────────────────────

_SUBPROTOCOL = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")          # RFC 9110 token
_CONNECTION_PATH = re.compile(r"/(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2}|/)*")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_HOST_FORBIDDEN = re.compile(r"[\s/\\@?#%\[\]:]")


@dataclass(frozen=True)
class ConnectionDefinition:
    """One declared network connection (`connections[]`).

    Its endpoint is the literal scheme, path and subprotocols here, with the
    host and port taken from two ordinary configuration settings. Nothing
    else (user information, query, fragment, headers, templates, secrets)
    can become part of the endpoint.
    """
    id: str
    transport: str
    scheme: str
    host_config: str
    port_config: str
    path: str
    subprotocols: tuple[str, ...] = ()

    def __post_init__(self):
        check_identifier(self.id, ("id",))
        if self.transport != "websocket":
            raise _fail(("transport",), "must be 'websocket'")
        if self.scheme not in ("ws", "wss"):
            raise _fail(("scheme",), "must be 'ws' or 'wss'")
        check_identifier(self.host_config, ("host_config",))
        check_identifier(self.port_config, ("port_config",))
        if self.host_config == self.port_config:
            raise _fail(("port_config",), "must differ from host_config")
        check_connection_path(self.path, ("path",))
        _set(self, "subprotocols", tuple(self.subprotocols))
        if len(self.subprotocols) > policy.CONNECTION_SUBPROTOCOLS_MAX:
            raise _fail(("subprotocols",),
                        f"must contain at most {policy.CONNECTION_SUBPROTOCOLS_MAX} items")
        for i, token in enumerate(self.subprotocols):
            check_subprotocol(token, ("subprotocols", i))
        check_unique(self.subprotocols, ("subprotocols",), "subprotocol")


def check_connection_path(value: Any, path: Path) -> str:
    """A literal absolute path: no query, fragment, template or spaces."""
    _check_str(value, path, policy.CONNECTION_PATH_MAX_CHARS)
    if not _CONNECTION_PATH.fullmatch(value):
        raise _fail(path, "must be a literal absolute path without query or fragment")
    return value


def check_subprotocol(value: Any, path: Path) -> str:
    _check_str(value, path, policy.CONNECTION_SUBPROTOCOL_MAX_CHARS)
    if not _SUBPROTOCOL.fullmatch(value):
        raise _fail(path, "must be a subprotocol token")
    return value


def canonical_host(value: Any, path: Path = ("host",)) -> str:
    """The canonical form of a configured host, compared exactly for consent.

    An IP literal (optionally in brackets) becomes its compressed form, an
    IPv4-mapped IPv6 address its IPv4 form; zone IDs are refused. A DNS name
    becomes lowercase ASCII (IDNA via the standard library codec) without a
    trailing dot. User information, ports, paths and numeric shorthand such
    as `127.1` (which resolvers would accept) are refused.
    """
    if not isinstance(value, str):
        raise _fail(path, "must be text")
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text or len(text) > policy.CONNECTION_HOST_MAX_CHARS or _CONTROL.search(text):
        raise _fail(path, "must be a host name or IP address")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        address = None
    if address is not None:
        if isinstance(address, ipaddress.IPv6Address):
            if address.scope_id:
                raise _fail(path, "must not include a zone ID")
            if address.ipv4_mapped is not None:
                address = address.ipv4_mapped
        return address.compressed
    if _HOST_FORBIDDEN.search(text):
        raise _fail(path, "must be only a host name or IP address")
    name = text[:-1] if text.endswith(".") else text
    if not name.isascii():
        try:
            name = name.encode("idna").decode("ascii")
        except UnicodeError:
            raise _fail(path, "is not a valid international host name") from None
    name = name.lower()
    labels = name.split(".")
    if len(name) > policy.CONNECTION_HOST_MAX_CHARS \
            or not all(_DNS_LABEL.fullmatch(label) for label in labels):
        raise _fail(path, "must be a host name or IP address")
    if all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
        raise _fail(path, "must be an IP address in standard form")
    return name


def check_endpoint(connection: ConnectionDefinition, value: Any, path: Path) -> MappingProxyType:
    """An owned endpoint of `connection`: literal parts equal its declaration."""
    owned = own_object(value, path)
    if set(owned) != {"scheme", "host", "port", "path", "subprotocols"}:
        raise _fail(path, "must contain exactly scheme, host, port, path and subprotocols")
    if owned["scheme"] != connection.scheme or owned["path"] != connection.path \
            or tuple(owned["subprotocols"]) != connection.subprotocols:
        raise _fail(path, "does not match the declared connection")
    if canonical_host(owned["host"], path + ("host",)) != owned["host"]:
        raise _fail(path + ("host",), "must be in canonical form")
    check_int(owned["port"], path + ("port",), 1, 65535)
    return owned


def endpoint_scope(connection: ConnectionDefinition, endpoint: Mapping[str, Any]) -> dict:
    """The scope a `network.websocket` grant stores: the connection and its endpoint."""
    return {"connection": connection.id, "endpoint": thaw_json(endpoint)}


def resolve_endpoint(connection: ConnectionDefinition, config: Any) -> dict | None:
    """The endpoint `config` selects, or None while a setting is unfinished.

    Raises INVALID_ARGUMENT at `/config/<setting>` for a value that cannot be
    an endpoint (configuration validation reports the rest).
    """
    if not isinstance(config, Mapping):
        return None
    host, port = config.get(connection.host_config), config.get(connection.port_config)
    if host is None or port is None:
        return None
    where = ("config", connection.host_config)
    if not isinstance(host, str):
        raise _fail(where, "must be a host name or IP address")
    host = canonical_host(host, where)
    if not is_json_int(port) or not 1 <= port <= 65535:
        raise _fail(("config", connection.port_config), "must be a port from 1 to 65535")
    return {"scheme": connection.scheme, "host": host, "port": port, "path": connection.path,
            "subprotocols": list(connection.subprotocols)}


@dataclass(frozen=True)
class ActionDispatch:
    """Host scheduling for one target: ordered or replace pending latest input."""
    mode: str
    key_arguments: tuple[str, ...]
    min_interval_ms: int | None = None

    def __post_init__(self):
        if self.mode not in ("ordered", "latest"):
            raise _fail(("dispatch", "mode"), "must be 'ordered' or 'latest'")
        keys = tuple(self.key_arguments)
        _set(self, "key_arguments", keys)
        if not 1 <= len(keys) <= 3:
            raise _fail(("dispatch", "key_arguments"), "must contain 1 to 3 fields")
        for i, key in enumerate(keys):
            check_identifier(key, ("dispatch", "key_arguments", i))
        check_unique(keys, ("dispatch", "key_arguments"), "field")
        if self.mode == "ordered" and self.min_interval_ms is not None:
            raise _fail(("dispatch", "min_interval_ms"), "is only used by latest mode")
        if self.mode == "latest":
            check_int(self.min_interval_ms, ("dispatch", "min_interval_ms"), 100, 5000)


@dataclass(frozen=True)
class ActionDefinition:
    """Static action: typed input/output schemas, permissions and completion text.

    Schemas are stored frozen after `validation.check_schema` accepted them.
    """
    id: str
    name: str
    description: str
    permissions: tuple[str, ...]
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    completion: str
    dispatch: ActionDispatch | None = None
    feedback: Mapping[str, Any] | None = None
    uses_menu: bool = False
    target_collection: str | None = None

    def __post_init__(self):
        check_bool(self.uses_menu, ("uses_menu",))
        if self.target_collection is not None:
            check_identifier(self.target_collection, ("target_collection",))
            target = self.input_schema.get("properties", {}).get("object_id", {})
            if target.get("type") != "string":
                raise _fail(("target_collection",), "requires a string object_id argument")
        if self.feedback is not None:
            from .overlay_manifest import validate_feedback
            _set(self, "feedback", own_object(validate_feedback(self.feedback), ("feedback",)))
        check_identifier(self.id, ("id",))
        check_text(self.name, ("name",))
        check_text(self.description, ("description",), policy.DESCRIPTION_MAX_CHARS)
        _set(self, "permissions", tuple(self.permissions))
        for i, permission in enumerate(self.permissions):
            check_identifier(permission, ("permissions", i))
        check_unique(self.permissions, ("permissions",), "permission")
        _set(self, "input_schema", own_object(self.input_schema, ("input_schema",)))
        _set(self, "output_schema", own_object(self.output_schema, ("output_schema",)))
        check_text(self.completion, ("completion",), policy.DESCRIPTION_MAX_CHARS)
        if self.dispatch is not None:
            if not isinstance(self.dispatch, ActionDispatch):
                raise _fail(("dispatch",), "is not a dispatch policy")
            schema = thaw_json(self.input_schema)
            required = set(schema.get("required", ()))
            properties = schema.get("properties", {})
            for key in self.dispatch.key_arguments:
                node = properties.get(key, {})
                if key not in required or node.get("type") not in (
                        "string", "integer", "number", "boolean"):
                    raise _fail(("dispatch", "key_arguments"),
                                "must name required scalar arguments")


@dataclass(frozen=True)
class CollectionDefinition:
    id: str
    name: str
    state_schema: Mapping[str, Any]
    state_ttl_seconds: int = policy.STATE_TTL_DEFAULT_S

    def __post_init__(self):
        check_identifier(self.id, ("id",))
        check_text(self.name, ("name",))
        _set(self, "state_schema", own_object(self.state_schema, ("state_schema",)))
        check_int(self.state_ttl_seconds, ("state_ttl_seconds",),
                  policy.STATE_TTL_MIN_S, policy.STATE_TTL_MAX_S)


@dataclass(frozen=True)
class EventDefinition:
    id: str
    payload_schema: Mapping[str, Any]

    def __post_init__(self):
        check_identifier(self.id, ("id",))
        _set(self, "payload_schema", own_object(self.payload_schema, ("payload_schema",)))


@dataclass(frozen=True)
class PackageManifest:
    """A validated author manifest. Build through `validation.validate_manifest`."""
    format_version: int
    protocol_version: int
    id: str
    version: str
    name: str
    author: str
    description: str
    target: str
    entrypoint: str
    config_schema_version: int
    config_schema: Mapping[str, Any]
    permissions: tuple[PermissionDefinition, ...]
    actions: tuple[ActionDefinition, ...]
    collections: tuple[CollectionDefinition, ...]
    events: tuple[EventDefinition, ...]
    secrets: tuple[SecretDefinition, ...] = ()
    auth: tuple[AuthDefinition, ...] = ()
    connections: tuple[ConnectionDefinition, ...] = ()
    web_sessions: tuple[WebSessionDefinition, ...] = ()
    reconnect_enabled: bool = False
    ui: Mapping[str, Any] | None = None
    compatibility: Mapping[str, Any] | None = None
    card: Mapping[str, Any] | None = None
    migrations: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self):
        check_bool(self.reconnect_enabled, ("reconnect", "enabled"))
        _set(self, "config_schema", own_object(self.config_schema, ("config_schema",)))
        for name in ("permissions", "actions", "collections", "events"):
            items = tuple(getattr(self, name))
            _set(self, name, items)
            check_unique(tuple(item.id for item in items), (name,), "ID")
        if self.ui is not None:
            from .overlay_manifest import validate_ui
            if self.format_version < 3:
                raise _fail(("ui",), "needs manifest format 3")
            _set(self, "ui", own_object(validate_ui(thaw_json(self.ui)), ("ui",)))
        from . import package_metadata
        if self.format_version >= 4:
            _set(self, "compatibility", own_object(package_metadata.compatibility(thaw_json(self.compatibility)), ("compatibility",)))
            icons = {i["id"] for i in (self.ui or {}).get("icons", ())}
            _set(self, "card", own_object(package_metadata.card(thaw_json(self.card), icons), ("card",)))
            _set(self, "migrations", tuple(own_object(m, ("migrations",))
                                           for m in package_metadata.migrations(thaw_json(self.migrations))))
        elif self.compatibility is not None or self.card is not None or self.migrations:
            raise _fail(("format_version",), "package compatibility, cards and migrations need manifest format 4")
        templates = {item["id"] for item in (self.ui or {}).get("templates", ())}
        for index, action in enumerate(self.actions):
            if action.target_collection is not None:
                if self.format_version < 3 or action.target_collection not in {c.id for c in self.collections}:
                    raise _fail(("actions", index, "target_collection"), "must name a declared collection in manifest format 3")
            if action.feedback is not None or action.uses_menu:
                if self.format_version < 3:
                    raise _fail(("actions", index), "UI declarations need manifest format 3")
                if action.feedback is not None and action.feedback["template"] not in templates:
                    raise _fail(("actions", index, "feedback"), "references an undeclared template")
                for requested, surface in ((action.feedback is not None, "toast"), (action.uses_menu, "menu")):
                    if requested and not any(p.id in action.permissions and p.kind == "ui.overlay" and
                                             p.scope["surface"] == surface for p in self.permissions):
                        raise _fail(("actions", index, "permissions"), "must declare an overlay permission for the requested surface")
        if any(p.kind == "ui.overlay" for p in self.permissions) and self.format_version < 3:
            raise _fail(("permissions",), "UI permissions need manifest format 3")
        self._check_secrets()
        self._check_connections()
        self._check_web_sessions()
        for index, item in enumerate(self.actions):
            if item.dispatch is not None and self.format_version < 2:
                raise _fail(("actions", index, "dispatch"), "needs manifest format 2")

    def _check_connections(self) -> None:
        """Connections, their settings and their `network.websocket` permissions agree.

        The host and port settings are root `config_schema` properties of type
        string and integer (so never secrets); each connection is named by
        exactly one permission, and every such permission names a connection.
        """
        connections = tuple(self.connections)
        _set(self, "connections", connections)
        path = ("connections",)
        if len(connections) > policy.CONNECTIONS_MAX:
            raise _fail(path, f"must contain at most {policy.CONNECTIONS_MAX} items")
        for index, connection in enumerate(connections):
            if not isinstance(connection, ConnectionDefinition):
                raise _fail(path + (index,), "must be a connection definition")
        check_unique(tuple(c.id for c in connections), path, "connection ID")
        schema = thaw_json(self.config_schema)
        properties = schema.get("properties") or {}
        definitions = schema.get("$defs") or {}

        def kind_of(name):
            node = properties.get(name)
            ref = node.get("$ref") if isinstance(node, dict) else None
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                node = definitions.get(ref[len("#/$defs/"):])
            return node.get("type") if isinstance(node, dict) else None
        for index, connection in enumerate(connections):
            for field_name, kind in (("host_config", "string"), ("port_config", "integer")):
                if kind_of(getattr(connection, field_name)) != kind:
                    raise _fail(path + (index, field_name),
                                f"must name a configuration setting of type {kind}")
        declared = {c.id for c in connections}
        users: dict[str, int] = {}
        for index, permission in enumerate(self.permissions):
            if permission.kind != "network.websocket":
                continue
            connection_id = permission.scope["connection"]
            if connection_id not in declared:
                raise _fail(("permissions", index, "scope", "connection"),
                            "does not name a declared connection")
            users[connection_id] = users.get(connection_id, 0) + 1
        for index, connection in enumerate(connections):
            if users.get(connection.id, 0) != 1:
                raise _fail(path + (index, "id"),
                            "must be named by exactly one 'network.websocket' permission")

    def _check_web_sessions(self) -> None:
        """Web sessions, their format-2 gate and their permissions agree.

        Each session is named by exactly one `network.web_session` permission,
        every such permission names a declared session, and a session id never
        collides with a configuration, secret or sign-in field. Declaring a web
        session, or a `network.web_session` permission, needs manifest format 2.
        """
        sessions = tuple(self.web_sessions)
        _set(self, "web_sessions", sessions)
        path = ("setup", "web_sessions")
        uses_kind = any(p.kind == "network.web_session" for p in self.permissions)
        if (sessions or uses_kind) and self.format_version < 2:
            raise _fail(path if sessions else ("permissions",),
                        "need manifest format 2")
        if len(sessions) > policy.WEB_SESSIONS_MAX:
            raise _fail(path, f"must contain at most {policy.WEB_SESSIONS_MAX} items")
        for index, session in enumerate(sessions):
            if not isinstance(session, WebSessionDefinition):
                raise _fail(path + (index,), "must be a web-session definition")
        check_unique(tuple(s.id for s in sessions), path, "web-session ID")
        config_fields = set(thaw_json(self.config_schema).get("properties") or {})
        reserved = config_fields | {s.id for s in self.secrets} | {a.id for a in self.auth}
        declared = {s.id for s in sessions}
        users: dict[str, int] = {}
        for index, permission in enumerate(self.permissions):
            if permission.kind != "network.web_session":
                continue
            session_id = permission.scope["session"]
            if session_id not in declared:
                raise _fail(("permissions", index, "scope", "session"),
                            "does not name a declared web session")
            users[session_id] = users.get(session_id, 0) + 1
        for index, session in enumerate(sessions):
            where = path + (index, "id")
            if session.id in reserved:
                raise _fail(where, "must not also be a configuration, secret or sign-in field")
            if users.get(session.id, 0) != 1:
                raise _fail(where, "must be named by exactly one 'network.web_session' permission")

    def web_session(self, session_id: str) -> WebSessionDefinition:
        return self._find(self.web_sessions, session_id, "web session")

    def web_session_permission(self, session_id: str) -> PermissionDefinition:
        """The one `network.web_session` permission that allows `session_id`."""
        self.web_session(session_id)
        for permission in self.permissions:
            if permission.kind == "network.web_session" \
                    and permission.scope["session"] == session_id:
                return permission
        raise DomainError(ErrorCode.INVALID_ARGUMENT, (), "is not an allowed web session")

    def connection(self, connection_id: str) -> ConnectionDefinition:
        return self._find(self.connections, connection_id, "connection")

    def connection_permission(self, connection_id: str) -> PermissionDefinition:
        """The one `network.websocket` permission that allows `connection_id`."""
        self.connection(connection_id)
        for permission in self.permissions:
            if permission.kind == "network.websocket" \
                    and permission.scope["connection"] == connection_id:
                return permission
        raise DomainError(ErrorCode.INVALID_ARGUMENT, (), "is not an allowed connection")

    def _check_secrets(self) -> None:
        """Secret fields and their disclosure permissions agree.

        Paths name the manifest JSON location (`/setup/secrets/i`). Each field
        is disclosed by exactly one `secret.read` permission scoped to it, and
        every such permission names a declared field.
        """
        secrets = tuple(self.secrets)
        _set(self, "secrets", secrets)
        path = ("setup", "secrets")
        if len(secrets) > policy.SECRETS_MAX:
            raise _fail(path, f"must contain at most {policy.SECRETS_MAX} items")
        for index, secret in enumerate(secrets):
            if not isinstance(secret, SecretDefinition):
                raise _fail(path + (index,), "must be a secret definition")
        check_unique(tuple(s.id for s in secrets), path, "secret ID")
        config_fields = set(thaw_json(self.config_schema).get("properties") or {})
        self._check_auth(config_fields, {s.id for s in secrets})
        declared = {s.id for s in secrets}
        readers: dict[str, int] = {}
        for index, permission in enumerate(self.permissions):
            if permission.kind != "secret.read":
                continue
            field_id = permission.scope["field"]
            if field_id not in declared:
                raise _fail(("permissions", index, "scope", "field"),
                            "does not name a declared secret field")
            readers[field_id] = readers.get(field_id, 0) + 1
        for index, secret in enumerate(secrets):
            where = path + (index, "id")
            if secret.id in config_fields:
                raise _fail(where, "must not also be a configuration field")
            if readers.get(secret.id, 0) != 1:
                raise _fail(where, "must be disclosed by exactly one 'secret.read' permission")

    def _find(self, items: tuple, item_id: str, what: str):
        for item in items:
            if item.id == item_id:
                return item
        raise DomainError(ErrorCode.INVALID_ARGUMENT, (), f"is not a declared {what}")

    def permission(self, permission_id: str) -> PermissionDefinition:
        return self._find(self.permissions, permission_id, "permission")

    def action(self, action_id: str) -> ActionDefinition:
        return self._find(self.actions, action_id, "action")

    def collection(self, collection_id: str) -> CollectionDefinition:
        return self._find(self.collections, collection_id, "collection")

    def _check_auth(self, config_fields: set, secret_ids: set) -> None:
        """Sign-in entries and their `auth.token` permissions agree (`/setup/auth/i`)."""
        entries = tuple(self.auth)
        _set(self, "auth", entries)
        path = ("setup", "auth")
        if len(entries) > policy.AUTH_MAX:
            raise _fail(path, f"must contain at most {policy.AUTH_MAX} items")
        for index, entry in enumerate(entries):
            if not isinstance(entry, AuthDefinition):
                raise _fail(path + (index,), "must be a sign-in definition")
        check_unique(tuple(a.id for a in entries), path, "sign-in ID")
        declared = {a.id for a in entries}
        readers: dict[str, int] = {}
        for index, permission in enumerate(self.permissions):
            if permission.kind != "auth.token":
                continue
            auth_id = permission.scope["auth"]
            if auth_id not in declared:
                raise _fail(("permissions", index, "scope", "auth"),
                            "does not name a declared sign-in")
            readers[auth_id] = readers.get(auth_id, 0) + 1
        for index, entry in enumerate(entries):
            where = path + (index, "id")
            if entry.id in config_fields:
                raise _fail(where, "must not also be a configuration field")
            if entry.id in secret_ids:
                raise _fail(where, "must not also be a secret field")
            if readers.get(entry.id, 0) != 1:
                raise _fail(where, "must be disclosed by exactly one 'auth.token' permission")

    def event(self, event_id: str) -> EventDefinition:
        return self._find(self.events, event_id, "event")

    def auth_entry(self, auth_id: str) -> AuthDefinition:
        return self._find(self.auth, auth_id, "sign-in")

    def auth_permission(self, auth_id: str) -> PermissionDefinition:
        """The one `auth.token` permission that discloses `auth_id`'s access token."""
        self.auth_entry(auth_id)
        for permission in self.permissions:
            if permission.kind == "auth.token" and permission.scope["auth"] == auth_id:
                return permission
        raise DomainError(ErrorCode.INVALID_ARGUMENT, (), "is not a disclosed sign-in")

    def secret(self, field_id: str) -> SecretDefinition:
        return self._find(self.secrets, field_id, "secret field")

    def secret_permission(self, field_id: str) -> PermissionDefinition:
        """The one `secret.read` permission that discloses `field_id`."""
        self.secret(field_id)
        for permission in self.permissions:
            if permission.kind == "secret.read" and permission.scope["field"] == field_id:
                return permission
        raise DomainError(ErrorCode.INVALID_ARGUMENT, (), "is not a disclosed secret field")


# ── Instances and grants ────────────────────────────────────────────────────

class GrantDecision(StrEnum):
    GRANTED = "granted"
    DENIED = "denied"


@dataclass(frozen=True)
class Grant:
    """One user decision for a declared permission and its approved scope."""
    permission_id: str
    scope: Mapping[str, Any]
    decision: GrantDecision

    def __post_init__(self):
        check_identifier(self.permission_id, ("permission_id",))
        _set(self, "scope", own_object(self.scope, ("scope",)))
        try:
            _set(self, "decision", GrantDecision(self.decision))
        except ValueError:
            raise _fail(("decision",), "must be 'granted' or 'denied'") from None

    def to_json(self) -> dict:
        return {"permission_id": self.permission_id, "scope": thaw_json(self.scope),
                "decision": self.decision.value}


@dataclass(frozen=True)
class GrantSet:
    """Grant snapshot at a host-owned revision. Absent permissions are denied."""
    revision: int = 0
    decisions: tuple[Grant, ...] = ()

    def __post_init__(self):
        check_int(self.revision, ("revision",), 0)
        _set(self, "decisions", tuple(self.decisions))
        check_unique(tuple(g.permission_id for g in self.decisions), ("decisions",), "permission")

    def to_json(self) -> dict:
        return {"revision": self.revision, "decisions": [g.to_json() for g in self.decisions]}


@dataclass(frozen=True)
class InstanceRecord:
    """Persisted local instance. Never carries a Ready flag or raw secrets.

    `secret_refs` lists configuration field IDs whose values live only in the
    host secret store. `secret_locations` optionally selects a protected storage
    namespace per field after a journaled update; workers still use instance IDs.
    """
    instance_id: str
    label: str
    pin: PackagePin
    config_schema_version: int
    config: Mapping[str, Any] = field(default_factory=dict)
    secret_refs: tuple[str, ...] = ()
    grants: GrantSet = field(default_factory=GrantSet)
    enabled: bool = False
    secret_locations: Mapping[str, str] = field(default_factory=dict)
    update_source: str | None = None

    def __post_init__(self):
        check_uuid(self.instance_id, ("instance_id",))
        check_text(self.label, ("label",))
        if not isinstance(self.pin, PackagePin):
            raise _fail(("pin",), "must be a package pin")
        check_int(self.config_schema_version, ("config_schema_version",), 1)
        _set(self, "config", own_object(self.config, ("config",)))
        _set(self, "secret_refs", tuple(self.secret_refs))
        for i, ref in enumerate(self.secret_refs):
            check_identifier(ref, ("secret_refs", i))
        check_unique(self.secret_refs, ("secret_refs",), "secret reference")
        if not isinstance(self.grants, GrantSet):
            raise _fail(("grants",), "must be a grant set")
        check_bool(self.enabled, ("enabled",))
        if self.update_source is not None:
            check_uuid(self.update_source, ("update_source",))
            if self.update_source == self.instance_id or self.enabled:
                raise _fail(("update_source",), "update setup must be a separate disabled instance")
        _set(self, "secret_locations", own_object(self.secret_locations, ("secret_locations",)))
        for key, namespace in self.secret_locations.items():
            check_identifier(key, ("secret_locations",))
            check_uuid(namespace, ("secret_locations", key))
            if key not in self.secret_refs:
                raise _fail(("secret_locations", key), "must name a referenced credential")

    def to_json(self) -> dict:
        return {
            "instance_id": self.instance_id, "label": self.label, "pin": self.pin.to_json(),
            "config_schema_version": self.config_schema_version,
            "config": thaw_json(self.config), "secret_refs": list(self.secret_refs),
            "grants": self.grants.to_json(), "enabled": self.enabled,
            **({"secret_locations": dict(self.secret_locations)} if self.secret_locations else {}),
            **({"update_source": self.update_source} if self.update_source else {}),
        }


class LifecycleState(StrEnum):
    """Runtime instance status. Never persisted as authoritative."""
    DISABLED = "disabled"
    STARTING = "starting"
    READY = "ready"
    NEEDS_SETUP = "needs_setup"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    STOPPING = "stopping"


# ── Discovery, state and events ─────────────────────────────────────────────

@dataclass(frozen=True)
class ObjectRecord:
    id: str
    collection_id: str
    label: str
    parent_id: str | None
    capabilities: tuple[str, ...]

    def __post_init__(self):
        check_opaque_id(self.id, ("id",))
        check_identifier(self.collection_id, ("collection_id",))
        check_text(self.label, ("label",))
        if self.parent_id is not None:
            check_opaque_id(self.parent_id, ("parent_id",))
            if self.parent_id == self.id:
                raise _fail(("parent_id",), "must not refer to the object itself")
        _set(self, "capabilities", tuple(self.capabilities))
        for i, action_id in enumerate(self.capabilities):
            check_identifier(action_id, ("capabilities", i))
        check_unique(self.capabilities, ("capabilities",), "capability")

    def to_json(self) -> dict:
        return {"id": self.id, "collection_id": self.collection_id, "label": self.label,
                "parent_id": self.parent_id, "capabilities": list(self.capabilities)}


@dataclass(frozen=True)
class DiscoveryPage:
    objects: tuple[ObjectRecord, ...]
    next_cursor: str | None

    def __post_init__(self):
        _set(self, "objects", tuple(self.objects))
        if len(self.objects) > policy.DISCOVERY_PAGE_MAX:
            raise _fail(("objects",), f"must contain at most {policy.DISCOVERY_PAGE_MAX} objects")
        check_unique(tuple(o.id for o in self.objects), ("objects",), "object ID")
        if self.next_cursor is not None:
            check_opaque_id(self.next_cursor, ("next_cursor",), policy.CURSOR_MAX_CHARS)
        parents = {o.id: o.parent_id for o in self.objects}
        for index, obj in enumerate(self.objects):
            seen, current = {obj.id}, obj.parent_id
            while current in parents:           # unknown parents stay unresolved
                if current in seen:
                    raise _fail(("objects", index, "parent_id"), "forms a parent cycle")
                seen.add(current)
                current = parents[current]

    def to_json(self) -> dict:
        return {"objects": [o.to_json() for o in self.objects], "next_cursor": self.next_cursor}


@dataclass(frozen=True)
class StateRecord:
    """Worker-reported state; the host adds observation times and staleness."""
    object_id: str
    revision: int
    values: Mapping[str, Any]
    available: bool

    def __post_init__(self):
        check_opaque_id(self.object_id, ("object_id",))
        check_int(self.revision, ("revision",), 0)
        _set(self, "values", own_object(self.values, ("values",)))
        check_bool(self.available, ("available",))

    def to_json(self) -> dict:
        return {"object_id": self.object_id, "revision": self.revision,
                "values": thaw_json(self.values), "available": self.available}


@dataclass(frozen=True)
class ObjectEvent:
    object_id: str
    event_id: str
    payload: Mapping[str, Any]

    def __post_init__(self):
        check_opaque_id(self.object_id, ("object_id",))
        check_identifier(self.event_id, ("event_id",))
        _set(self, "payload", own_object(self.payload, ("payload",)))


# ── Worker envelopes ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ErrorInfo:
    """Wire error object: bounded user-safe message, never an exception dump."""
    code: ErrorCode
    message: str
    retryable: bool = False
    outcome_unknown: bool = False

    def __post_init__(self):
        try:
            _set(self, "code", ErrorCode(self.code))
        except ValueError:
            raise _fail(("code",), "is not a known error code") from None
        check_text(self.message, ("message",), policy.ERROR_TEXT_MAX_CHARS)
        check_bool(self.retryable, ("retryable",))
        check_bool(self.outcome_unknown, ("outcome_unknown",))

    def to_json(self) -> dict:
        return {"code": self.code.value, "message": self.message,
                "retryable": self.retryable, "outcome_unknown": self.outcome_unknown}


@dataclass(frozen=True)
class Request:
    id: str
    method: str
    timeout_ms: int
    params: Mapping[str, Any]

    def __post_init__(self):
        check_request_id(self.id, ("id",))
        check_identifier(self.method, ("method",))
        check_int(self.timeout_ms, ("timeout_ms",), 1, policy.REQUEST_TIMEOUT_MAX_MS)
        _set(self, "params", own_object(self.params, ("params",)))

    def to_wire(self) -> dict:
        return {"v": policy.PROTOCOL_VERSION, "kind": "request", "id": self.id,
                "method": self.method, "timeout_ms": self.timeout_ms,
                "params": thaw_json(self.params)}


@dataclass(frozen=True)
class Response:
    """Terminal response: success carries `result`, failure carries `error`."""
    id: str
    ok: bool
    result: Any = None
    error: ErrorInfo | None = None

    def __post_init__(self):
        check_request_id(self.id, ("id",))
        check_bool(self.ok, ("ok",))
        if self.ok:
            if self.error is not None:
                raise _fail(("error",), "must be absent from a success response")
            _set(self, "result", freeze_json(copy_json(thaw_json(self.result), ("result",))))
        else:
            if self.result is not None:
                raise _fail(("result",), "must be absent from an error response")
            if not isinstance(self.error, ErrorInfo):
                raise _fail(("error",), "is required in an error response")

    def to_wire(self) -> dict:
        wire = {"v": policy.PROTOCOL_VERSION, "kind": "response", "id": self.id, "ok": self.ok}
        if self.ok:
            wire["result"] = thaw_json(self.result)
        else:
            wire["error"] = self.error.to_json()
        return wire


@dataclass(frozen=True)
class Event:
    name: str
    seq: int
    data: Mapping[str, Any]

    def __post_init__(self):
        check_identifier(self.name, ("name",))
        check_int(self.seq, ("seq",), 1)
        _set(self, "data", own_object(self.data, ("data",)))

    def to_wire(self) -> dict:
        return {"v": policy.PROTOCOL_VERSION, "kind": "event", "name": self.name,
                "seq": self.seq, "data": thaw_json(self.data)}


Message = Request | Response | Event

REQUEST_ID_PREFIXES = {"host": "h-", "worker": "w-"}
# Each side numbers its requests 1, 2, 3, ... in issue order within a session.
# Numbers never repeat; gaps are allowed. Receivers enforce the increase.
_REQUEST_ID = re.compile(r"[hw]-[1-9][0-9]{0,17}")


def check_request_id(value: Any, path: Path) -> str:
    check_opaque_id(value, path)
    if not _REQUEST_ID.fullmatch(value):
        raise _fail(path, "must be 'h-' or 'w-' followed by a positive decimal number")
    return value


def request_number(request_id: str) -> int:
    """The issue number of a checked request ID (`h-12` → 12)."""
    return int(request_id[2:])
