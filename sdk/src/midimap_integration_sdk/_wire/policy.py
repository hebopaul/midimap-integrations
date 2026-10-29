"""Centralized integration limits (worker protocol "Central policy defaults").

These are initial product limits, not OS limits. Change them only together with
boundary tests and the limits table in docs/integration-packs.md.
Durations are seconds of host monotonic time unless the name says otherwise.

Part of the pure wire-contract set (`policy`, `contracts`, `validation`,
`framing`, `overlay_spec`) that the author SDK ships as a byte-identical copy; keep these
modules free of app imports and use relative imports only.
"""
from typing import Final

KIB: Final = 1024
MIB: Final = 1024 * KIB

# ── Versions and targets ────────────────────────────────────────────────────
MANIFEST_FORMAT_VERSION: Final = 1
SUPPORTED_MANIFEST_FORMAT_VERSIONS: Final = (1, 2, 3, 4)
PROTOCOL_VERSION: Final = 1
INSTANCE_RECORD_SCHEMA_VERSION: Final = 2
SUPPORTED_TARGETS: Final = ("windows-x64",)

# ── Archives and manifests ──────────────────────────────────────────────────
ARCHIVE_COMPRESSED_MAX_BYTES: Final = 256 * MIB
ARCHIVE_EXPANDED_MAX_BYTES: Final = 512 * MIB
ARCHIVE_ENTRIES_MAX: Final = 4096
ARCHIVE_FILE_MAX_BYTES: Final = 256 * MIB
ARCHIVE_PATH_MAX_CHARS: Final = 512          # archive entry path, '/'-separated
ARCHIVE_PATH_SEGMENT_MAX_CHARS: Final = 255  # NTFS component limit
MANIFEST_MAX_BYTES: Final = 256 * KIB

# ── Messages and text ───────────────────────────────────────────────────────
MESSAGE_MAX_BYTES: Final = 1 * MIB          # including the terminating newline
JSON_DEPTH_MAX: Final = 32                  # source JSON and expanded schemas
IDENTIFIER_MAX_CHARS: Final = 128
ERROR_TEXT_MAX_CHARS: Final = 512
DISPLAY_NAME_MAX_CHARS: Final = 128         # names, labels, schema titles
DESCRIPTION_MAX_CHARS: Final = 1024         # descriptions, completion text
CURSOR_MAX_CHARS: Final = 1024              # opaque discovery cursors

# ── Processes and capacity ──────────────────────────────────────────────────
ENABLED_WORKERS_MAX: Final = 8
ACTION_OPERATIONS_MAX: Final = 32
ACTION_OPERATIONS_PER_INSTANCE_MAX: Final = 8
WORKER_HOST_OPERATIONS_MAX: Final = 8       # per worker, separate from actions
HOST_CONTROL_REQUESTS_MAX: Final = 8        # per worker, separate from actions
WRITE_QUEUE_MAX: Final = 64                 # control/write messages per session
EVENT_QUEUE_MAX: Final = 256                # per instance
DISCOVERY_PAGE_MAX: Final = 100             # objects or state records per page
CACHED_OBJECTS_MAX: Final = 5000            # per instance
STREAM_READ_CHUNK_BYTES: Final = 64 * KIB   # one pipe read, both directions
WORKER_BACKGROUND_TASKS_MAX: Final = 8      # SDK-owned background tasks per worker
WORKER_LOG_RECORD_MAX_CHARS: Final = 1024   # one stderr log record from the SDK

# ── Deadlines ───────────────────────────────────────────────────────────────
STARTUP_TIMEOUT_S: Final = 15
DISCOVERY_TIMEOUT_S: Final = 10             # discovery.list and state.snapshot
ACTION_TIMEOUT_DEFAULT_S: Final = 30
ACTION_TIMEOUT_MIN_S: Final = 1
ACTION_TIMEOUT_MAX_S: Final = 300
SETTLEMENT_GRACE_S: Final = 5
HOST_SERVICE_TIMEOUT_MAX_S: Final = 30
SHUTDOWN_GRACE_S: Final = 5
WORKER_SHUTDOWN_REPLY_MARGIN_S: Final = 1   # SDK answers shutdown this long before its deadline
REQUEST_TIMEOUT_MAX_MS: Final = ACTION_TIMEOUT_MAX_S * 1000

# ── Secrets ─────────────────────────────────────────────────────────────────
SECRETS_MAX: Final = 8                      # declared secret fields per package
SECRET_VALUE_MAX_BYTES: Final = 2048        # UTF-8; below Credential Manager's 2560-byte blob limit
SECRET_RESERVED_PREFIX: Final = "host."     # secret IDs reserved for host-run sign-in tokens

# ── Browser web sessions (manifest format 2; host.web_session.* services) ────
WEB_SESSIONS_MAX: Final = 1                 # declared web-session instances per package
WEB_SESSION_PROFILES: Final = frozenset({"google_home_web"})  # host-implemented profiles only
WEB_SESSION_STORAGE_PREFIX: Final = "host.websession."   # reserved secret-store field namespace
WEB_SESSION_BUNDLE_MAX_BYTES: Final = 64 * KIB           # serialized (ASCII JSON) session bundle
WEB_SESSION_CHUNK_BYTES: Final = 2 * KIB                 # one stored chunk (below the 2560 blob cap)
WEB_SESSION_CHUNKS_MAX: Final = WEB_SESSION_BUNDLE_MAX_BYTES // WEB_SESSION_CHUNK_BYTES  # 32
WEB_SESSION_INDEX_MAX_BYTES: Final = 512
WEB_SESSION_REQUEST_MAX_BYTES: Final = 32 * KIB          # one signed request body
WEB_SESSION_RESPONSE_MAX_BYTES: Final = 512 * KIB        # one response body
WEB_SESSION_RESPONSE_HEADER_MAX_BYTES: Final = 32 * KIB  # the response header block
WEB_SESSION_HTTP_TIMEOUT_S: Final = 10                   # total monotonic deadline per request
WEB_SESSION_HTTP_STEP_S: Final = 5                       # longest single socket operation

# ── Sign-in (C4.3) ──────────────────────────────────────────────────────────
AUTH_MAX: Final = 4                         # setup.auth entries per package
AUTH_SCOPES_MAX: Final = 32
AUTH_CLIENT_ID_MAX_CHARS: Final = 256
AUTH_URL_MAX_CHARS: Final = 2048
AUTH_VERIFICATION_HOSTS_MAX: Final = 4
AUTH_ACCESS_TOKEN_MAX_BYTES: Final = 8 * KIB
AUTH_TOKEN_SET_MAX_BYTES: Final = 16 * KIB  # serialized ASCII JSON
AUTH_CHUNK_BYTES: Final = 2048
AUTH_CHUNKS_MAX: Final = 8
AUTH_INDEX_MAX_BYTES: Final = 512
AUTH_BROWSER_TIMEOUT_S: Final = 300
AUTH_DEVICE_TIMEOUT_MAX_S: Final = 900
AUTH_DEVICE_INTERVAL_DEFAULT_S: Final = 5
AUTH_USER_CODE_MAX_CHARS: Final = 32
AUTH_HTTP_TIMEOUT_S: Final = 15             # total monotonic deadline per request
AUTH_HTTP_STEP_S: Final = 5                 # longest single socket operation
AUTH_RESPONSE_MAX_BYTES: Final = 64 * KIB
AUTH_RUNNER_THREADS: Final = 2
AUTH_RUNNER_QUEUE_MAX: Final = 4
AUTH_REFRESH_SKEW_S: Final = 60
AUTH_SAVE_RETRIES_MAX: Final = 5            # per entry per service lifetime
AUTH_SAVE_RETRY_SPACING_S: Final = 30
AUTH_LISTENER_CONNECTIONS_MAX: Final = 8
AUTH_LISTENER_REQUEST_MAX_BYTES: Final = 8 * KIB
AUTH_LISTENER_TIMEOUT_S: Final = 10

# ── Connections (C5, CONNECTIONS.md) ────────────────────────────────────────
CONNECTIONS_MAX: Final = 1                  # declared connections per package
CONNECTION_PATH_MAX_CHARS: Final = 1024
CONNECTION_SUBPROTOCOLS_MAX: Final = 4
CONNECTION_SUBPROTOCOL_MAX_CHARS: Final = 128
CONNECTION_HOST_MAX_CHARS: Final = 253
WEBSOCKET_MESSAGE_MAX_BYTES: Final = 128 * KIB   # UTF-8, one complete text message
WEBSOCKET_QUEUE_MAX: Final = 16             # received messages waiting per connection
WEBSOCKET_QUEUE_MAX_BYTES: Final = 1 * MIB
WEBSOCKET_SEND_WAITING_MAX: Final = 8       # sends waiting behind the one being written
WEBSOCKET_RECEIVE_WAIT_S: Final = 25        # longest empty wait of one receive
WEBSOCKET_OPEN_TIMEOUT_S: Final = 10
WEBSOCKET_CLOSE_TIMEOUT_S: Final = 3
WEBSOCKET_PING_INTERVAL_S: Final = 20
WEBSOCKET_PING_TIMEOUT_S: Final = 20
WEBSOCKET_LIBRARY_QUEUE_FRAMES: Final = 16  # the library's own receive buffer (flow control)
RESOLVER_OUTSTANDING_MAX: Final = 4         # name lookups running or queued (all instances)

# ── State and presentation ──────────────────────────────────────────────────
STATE_TTL_MIN_S: Final = 1
STATE_TTL_MAX_S: Final = 3600
STATE_TTL_DEFAULT_S: Final = 30
UI_REFRESH_MAX_PER_S: Final = 10

# ── Overlay spec v1 (runtime enforcement arrives with the overlay host) ─────
OVERLAY_SPEC_VERSIONS: Final = (1,)
OVERLAY_SPEC_MAX_BYTES: Final = 16 * KIB
OVERLAY_ELEMENTS_MAX: Final = 64
OVERLAY_DEPTH_MAX: Final = 6
OVERLAY_TEXT_MAX_CHARS: Final = 256
OVERLAY_QR_PAYLOAD_MAX_CHARS: Final = 2048
OVERLAY_PATCHES_MAX_PER_S: Final = 30
OVERLAY_TOASTS_PER_INSTANCE_MAX: Final = 4
OVERLAY_ASSET_MAX_BYTES: Final = 256 * KIB
OVERLAY_ASSET_DIMENSION_MAX: Final = 512
OVERLAY_ASSETS_PER_INSTANCE_MAX: Final = 32
OVERLAY_STICKY_MAX_MS: Final = 600_000
OVERLAY_DURATION_MAX_MS: Final = 600_000
OVERLAY_SPARKLINE_POINTS_MAX: Final = 64
MENU_PAGE_ITEMS_MAX: Final = 200
MENU_PAGES_MAX: Final = 32  # additional pages, excluding root
MENU_SLIDER_STEPS_MAX: Final = 10_000
MENU_SPEC_MAX_BYTES: Final = 64 * KIB
MENU_FRAGMENT_MAX_BYTES: Final = 64 * KIB
MENU_ITEM_ELEMENTS_MAX: Final = 8  # item, optional visual, and bounded fallback trees
MENU_ELEMENTS_MAX: Final = 1 + MENU_PAGE_ITEMS_MAX * MENU_ITEM_ELEMENTS_MAX + OVERLAY_ELEMENTS_MAX
MENU_CURSOR_MAX_CHARS: Final = 1024
MENU_TOTAL_ITEMS_MAX: Final = 1_000_000

# ── Live discovery and state freshness (C5 step 3b, CONNECTIONS.md E1/E2) ──
DISCOVERY_DEBOUNCE_S: Final = 0.1           # coalescing window after objects.changed
DISCOVERY_SCAN_PAGES_MAX: Final = 100       # pages one complete collection scan may read
DISCOVERY_PACE_MAX_S: Final = 5             # longest pause between rescans that did not finish
BACKGROUND_QUERIES_MAX: Final = 4           # host-started scans/refreshes in flight, all instances
STATE_REFRESH_COALESCE_S: Final = 5         # refresh batches take objects due this much later
STATE_REFRESH_RETRY_MAX_S: Final = 30       # longest pause before retrying an unanswered refresh
STATE_INTERESTS_MAX: Final = 64             # open state consumers per service
