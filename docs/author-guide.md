# Authoring integration packages (planned)

This describes the replacement package system. A developer-preview
Integrations tab imports packages and sets up instances and permissions, and
mappings and macros can run their actions; see the
[Integrations tab](integration-service.md#integrations-tab). Secret fields and
sign-in flows are not available yet.
Implemented so far:

- in `src/integrations/`: manifest, schema, message and limit validation (golden
  fixtures in `src/tests/fixtures/integrations/`), the package repository, the
  local instance store, the host side of the worker protocol, bounded pipe
  transport and a generation-owning worker supervisor;
- in `src/platform/`: the Windows process launcher with owned Job Objects and
  privilege verification before resuming the worker;
- in `sdk/python/`: the Python author SDK (`midimap_integration_sdk`), the
  simulator example, and the frozen-worker build recipe.

The SDK has been tested against the host protocol engine in process, over real
pipes under development Python, and as a frozen simulator behind the supervisor
on ordinary Windows 11. See [process verification and remaining gates](integration-processes.md).
Elevated-host and clean Windows without Python still need real verification.
The former declarative `.midimap-pack` YAML format is superseded; do not use it
for new work or assume it can be imported by this design.

## What you build

Write ordinary integration code that authenticates with a provider, discovers
objects, reads state, emits events, and implements typed actions. MIDImap owns
setup presentation, local instances, permissions, secret storage, process
supervision, and mapping/macro execution. Do not bundle a macro runner.

The initial author SDK uses Python, but a distributable package contains a frozen
Windows x64 worker and its dependencies. Only authors need development tools;
users import the completed archive and follow setup. Packages execute trusted
author code; the permission system is not a Windows sandbox. Permissions
control access to MIDImap-owned host services only. They cannot stop a package
executable from opening its own sockets, reading files available to the Windows
user, or calling Windows APIs, so users must trust the package author.

## Package layout

Use a ZIP with extension `.midimap-integration`, containing these files at root:

```text
manifest.json
README.md
LICENSE
worker/
  integration-worker.exe
  _internal/                 # frozen worker dependencies when required
```

Do not add an enclosing directory. Do not include virtual environments, source
checkout caches, credentials, machine configuration, install hooks, or scripts
that download dependencies on the user's machine. An optional icon can be added
only once its safe image-loading contract is implemented; it is not a foundation
requirement.

The host rejects an archive, before writing any of it, when an entry:

- uses an absolute, drive, UNC, or `..` path, a backslash, an empty or `.`
  segment, a `:` (alternate data stream), a Windows device name such as `CON` or
  `nul.txt`, or a segment ending in a space or dot;
- collides with another entry when names are compared case-insensitively, uses
  a file name that is also needed as a directory, or spells a directory with
  different letter case than another entry (`worker/` and `Worker/`);
- has a name that is not valid UTF-8 when the entry is flagged as UTF-8;
- is a symbolic link, junction/reparse point, or other special file;
- is encrypted or uses compression other than stored or deflate;
- exceeds the archive limits listed in the protocol reference below.

`manifest.json` must exist at the root, and `entrypoint` must name a file in the
archive with exactly matching case. A missing `README.md` or `LICENSE` produces
an import warning, not a rejection.

The host hashes the archive; do not put a self-referential archive digest inside
the manifest. Same ID/version with different bytes is a conflict. Publish a new
version when changing contents, including documentation in a released archive.
Keep build inputs pinned and publish the exact tested archive.

## Manifest example

This is the complete minimal simulator manifest proposed for the foundation.
Its `test.probe` permission works only in the test harness. It is not a service
that production packages can claim.

```json
{
  "format_version": 1,
  "protocol_version": 1,
  "id": "org.midimap.simulator",
  "version": "0.1.0",
  "name": "Integration Simulator",
  "author": "MIDImap development team",
  "description": "Exercises the integration foundation without external services.",
  "target": "windows-x64",
  "entrypoint": "worker/integration-worker.exe",
  "config_schema_version": 1,
  "config_schema": {
    "type": "object",
    "properties": {
      "label": {"type": "string", "minLength": 1, "maxLength": 80}
    },
    "required": ["label"],
    "additionalProperties": false
  },
  "permissions": [
    {
      "id": "simulator.probe",
      "kind": "test.probe",
      "description": "Exercise the host's synthetic permission boundary.",
      "scope": {"resource": "simulator"},
      "required_for_initialization": false
    }
  ],
  "actions": [
    {
      "id": "set_level",
      "name": "Set simulated level",
      "description": "Change a simulated object's level and return the confirmed value.",
      "permissions": ["simulator.probe"],
      "input_schema": {
        "type": "object",
        "properties": {
          "object_id": {"type": "string", "minLength": 1, "maxLength": 128},
          "level": {"type": "integer", "minimum": 0, "maximum": 100}
        },
        "required": ["object_id", "level"],
        "additionalProperties": false
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "object_id": {"type": "string", "minLength": 1, "maxLength": 128},
          "level": {"type": "integer", "minimum": 0, "maximum": 100}
        },
        "required": ["object_id", "level"],
        "additionalProperties": false
      },
      "completion": "The simulator has applied the level and returned the resulting state."
    }
  ],
  "collections": [
    {
      "id": "devices",
      "name": "Simulated devices",
      "state_ttl_seconds": 30,
      "state_schema": {
        "type": "object",
        "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
        "required": ["level"],
        "additionalProperties": false
      }
    }
  ],
  "events": [
    {
      "id": "level_changed",
      "payload_schema": {
        "type": "object",
        "properties": {"level": {"type": "integer", "minimum": 0, "maximum": 100}},
        "required": ["level"],
        "additionalProperties": false
      }
    }
  ]
}
```

Unknown manifest fields fail validation. IDs are stable, bounded identifiers, not
localized display names.

Schemas use a small JSON Schema Draft 2020-12 subset: `type` (one of object,
array, string, boolean, integer, number, null), `properties`, `required`,
`additionalProperties` (boolean), `items`, `minItems`, `maxItems`, `minLength`,
`maxLength`, `minimum`, `maximum`, `enum`, root `$defs`, local `$ref` to
`#/$defs/<name>`, `title`, and `description`. Other keywords, union types, and
remote references are rejected. Every object schema must state
`additionalProperties`, and every array schema must state `items`. Action
`input_schema` and `config_schema` must be objects with `additionalProperties:
false`. `integer` accepts only whole JSON numbers written without a fraction,
so `25.0` and `true` are both rejected. Secrets never belong in
`config_schema`: declare them as secret fields (below).

### Compatibility, cards and configuration migrations (format 4)

Formats 1–3 remain supported. New packages can use format 4 to declare their
minimum host version and build, required capabilities, a mandatory card icon,
and optional configuration migrations. These fields are validated by both
MIDImap and the Python SDK. For example, alongside the other manifest fields:

```json
{
  "format_version": 4,
  "compatibility": {
    "min_midimap_version": "0.8.0",
    "min_midimap_build": "20261029002",
    "capabilities": ["network.websocket", "ui.card", "migrations.config"]
  },
  "ui": {
    "spec_version": 1,
    "icons": [{"id": "brand", "path": "icons/brand.png"}]
  },
  "card": {
    "icon": "brand",
    "accent": "#64C5A8",
    "angle": 135,
    "gradient": [
      {"position": 0, "color": "#164238"},
      {"position": 0.4, "color": "#336BA3"},
      {"position": 1, "color": "#7754AB"}
    ]
  },
  "migrations": [{
    "from_version": "0.1.0",
    "config": [{"op": "rename", "from": "hostname", "to": "host"}]
  }]
}
```

Compatibility checks run before installation, instance creation, startup and
update preparation. A source checkout with an unknown application version
cannot satisfy a declared minimum version. Capabilities describe implemented
host features, independently of the package's protocol/format versions. Current
names are `network.websocket`, `network.web_session`, `auth.token`, `secret.read`,
`ui.overlay`, `midi.bindings`, `package.updates`, `migrations.config`,
`migrations.actions`, `updates.setup`, `store.catalog` and `ui.card`.

The icon must name a packaged PNG declared in `ui.icons`. Colors use `#RRGGBB`.
A gradient has 2–16 stops with strictly increasing positions starting at 0 and
ending at 1; angle is 0–360 degrees, default 135. These are passive display
declarations, never executable styles. Existing packages retain their fallback
icon/monogram and accent.

Each migration names one exact previous package version; there is no implicit
chain through intermediate releases. Up to 32 previous versions may be listed,
each with at most 128 operations on top-level configuration fields:

- `rename` or `copy`: `from` and `to`; absent source is ignored. An existing
  destination is never overwritten.
- `default`: `to` and a JSON `value`; applies only when the field is absent.
- `remove`: `from`; absent field is ignored.

Migrated settings appear in the update review and are validated against the
new schema. Users may manually correct them, including resolving a migration
collision. Changed endpoint scopes require permission review even if an author
migrated the settings. The update journal protects configuration changes along
with the instance version. Configuration operations alone do not rewrite mapping arguments,
secrets or sign-in credentials. Secret values can instead be entered in protected
update fields; changed OAuth/browser-session authority uses a separate disabled
setup draft, leaving the current instance running until Apply.

An exact-version migration may also declare action and argument changes:

```json
{
  "from_version": "0.1.0",
  "config": [],
  "actions": [{
    "from": "set_light",
    "to": "set_power",
    "arguments": [{"op": "rename", "from": "enabled", "to": "on"}]
  }]
}
```

Action sources must be unique (at most 128). Argument operations use the same
bounded `rename`, `copy`, `default` and `remove` vocabulary. They apply only to
selected instances and their declared previous version, including individual
macro steps. Numeric MIDI binding field names follow rename/copy/remove changes;
their ranges still must validate against the new action. Collisions keep the
original mapping and produce a repair note. The review shows migrated and
incompatible items before Apply. Mapping changes/deletions and package records
share a durable recovery journal; arbitrary migration scripts are never executed.

### Curated public distribution

MIDImap's Integration Store browses a public Cloudflare catalog and downloads
immutable, SHA-256-addressed archives. Store releases need format 4, a valid
packaged PNG icon, application compatibility, source access, and explicit review/
tested-version coverage tied to the exact archive. Compatibility is not evidence
of testing against a real provider. Development/synthetic-only coverage is shown
as such. Downloads always pass through package inspection and permission review;
updates remain explicit and preserve configured instance IDs.

Maintainers prepare releases with `python -m scripts.publish_integration` using
successful digest-matched qualification evidence. The default produces reviewable
metadata and a source export under ignored `build/`; `--publish` is explicit and
uses the separate machine-local publisher credential. Source exports contain the
integration and MIT SDK only. Public Cloudflare source ZIPs work without GitHub
authentication. Browse sources and contribute improvements at
[midimap-integrations](https://github.com/hebopaul/midimap-integrations).
See `server/README.md` for deployment and publishing.

### Secret fields

A package that needs a password, API key or token declares it in an optional
top-level `setup` object, and discloses each one through its own `secret.read`
permission. Add both to the manifest:

```json
"setup": {"secrets": [
  {"id": "server_password", "name": "Server password",
   "description": "The password set in OBS under WebSocket Server Settings.",
   "required": true}
]},
"permissions": [
  {"id": "obs.password", "kind": "secret.read",
   "description": "Read the saved server password to sign in.",
   "scope": {"field": "server_password"}, "required_for_initialization": true}
]
```

- **`id`:** a lowercase identifier, unique among secret fields. It must not
  also be a `config_schema` property, and must not start with `host.`, which is
  reserved for sign-in that MIDImap runs itself.
- **`name` / `description`:** shown in setup (128 / 1024 characters).
- **`required`:** a required field blocks starting until it is saved. An
  optional one never blocks setup; reading it while it is not saved fails only
  that call.
- **Limits:** at most 8 fields per package. A value is nonempty text without
  NUL, at most 2048 UTF-8 bytes.
- **Disclosure:** every field must be named by exactly one `secret.read`
  permission scoped `{"field": "<id>"}`, and every such permission must name a
  declared field. Your worker reads a value only through
  `setup.host.read_secret(...)`, and only while the user has granted that
  permission. From an action, the action must also list the permission.

The user enters values in setup. MIDImap stores them in Windows Credential
Manager for that integration instance, never shows them again, and deletes
them when the instance is removed. They never appear in configuration,
exports, logs or the worker's arguments or environment.

### Sign-in (OAuth)

For services that use OAuth 2.0 sign-in, declare a `setup.auth` entry beside
`setup.secrets`. MIDImap runs the whole sign-in itself: it opens the browser
or shows a device code, receives the response on `127.0.0.1`, exchanges the
code with PKCE, stores and renews the tokens, and signs out. Your worker never
sees a code, a refresh token or a provider response; it asks for the current
access token with `setup.host.access_token(...)`.

```json
"setup": {"auth": [{
  "id": "account", "name": "Example account",
  "description": "Sign in so the integration can read your channel.",
  "required": true, "client_id": "public-client-id", "scopes": ["channel:read"],
  "issuer": "https://id.example.com",
  "token_url": "https://id.example.com/oauth2/token",
  "revocation_url": "https://id.example.com/oauth2/revoke",
  "methods": {
    "browser": {"authorize_url": "https://id.example.com/oauth2/authorize",
                "redirect_path": "/callback"},
    "device": {"device_authorization_url": "https://id.example.com/oauth2/device",
               "verification_hosts": ["www.example.com"]}
  }
}]},
"permissions": [
  {"id": "example.account", "kind": "auth.token",
   "description": "Use your Example account.", "scope": {"auth": "account"},
   "required_for_initialization": false}
]
```

- **`id`:** unique across `setup.auth` and `setup.secrets`; not a
  `config_schema` property; must not start with `host.`.
- **`client_id`:** your public client ID. Packages are public clients: there is
  no client secret field.
- **URLs:** absolute `https` with a DNS host (no IP address, `localhost`,
  user info, query or fragment). Users see each one's host at consent.
- **`issuer`:** declare it only if the provider sends `iss` in its sign-in
  responses (RFC 9207); MIDImap then requires it to match exactly.
- **`methods`:** at least one of `browser` and `device`. The browser redirect is
  always `http://127.0.0.1:<port><redirect_path>`; providers that accept only
  `localhost` redirects are not supported yet. `verification_hosts` lists the
  1–4 hosts a device code's verification address may use.
- **Limits:** at most 4 entries; at most 32 scopes.
- **Disclosure:** each entry is named by exactly one `auth.token` permission
  scoped `{"auth": "<id>"}`. From an action, the action must also list it.

Renewal happens on demand when your worker asks for a token that is about to
expire. If MIDImap cannot confirm a renewal, it never reuses that sign-in and
asks the user to sign in again once the access token expires. Signing out
deletes the saved sign-in first and then, when no other integration may share
it, asks the provider to revoke it.

Format, protocol, and configuration schema versions have different purposes:
archive interpretation, wire interoperability, and local configuration migration.
The package version identifies a released set of code and capabilities. Do not
change one of these numbers to conceal an incompatible change in another.

### Browser sessions (format 2)

Some providers have no suitable OAuth client and are reached only through a
signed-in browser session. A **format 2** package may declare one host-run web
session beside `setup.secrets` and `setup.auth`:

```json
"setup": {"web_sessions": [
  {"id": "google_home", "profile": "google_home_web", "name": "Google Home"}
]},
"permissions": [
  {"id": "gh.session", "kind": "network.web_session",
   "description": "Use the connected Google Home account.",
   "scope": {"session": "google_home"}, "required_for_initialization": true}
]
```

- **`format_version`:** `web_sessions` and the `network.web_session` permission
  kind need manifest format 2. An older host rejects a format-2 manifest before
  launching the worker; format 1 packages validate and behave exactly as before.
- **`profile`:** a host-implemented browser profile from a fixed allowlist, not
  anything the package supplies. The package provides no browser JavaScript,
  origins or RPC paths. V1 allows one web session per package.
- **`id`:** a lowercase identifier, never reused as a configuration, secret or
  sign-in field, and not starting with the reserved `host.` prefix. Each session
  is named by exactly one `network.web_session` permission, and each such
  permission names a declared session. From an action, the action must list it.

MIDImap owns the whole session: an isolated browser profile, the captured
cookies, request signing and HTTPS execution. Consent identifies the access
through the signed-in account and the fixed service; it is not a provider-issued
narrow grant, and the underlying session is broader. Your worker never receives
cookies or a general-purpose token. It calls
`await setup.host.web_session_info(session_id)` for non-secret status
(connection, account label, selected home, a session epoch) and
`await setup.host.web_session_request(session_id, operation, body)` to run one
of the profile's fixed operations, which the host maps to a fixed path, signs
and forwards. The result carries the response status and parsed data, never
headers or credentials, and a returned status is acceptance, not physical
confirmation.

The setup browser uses a per-instance profile under the user's local MIDImap
application data. Human sign-in starts without remote debugging. After sign-in,
**Find homes** reopens that same profile with inspection enabled. The profile
contains sign-in state and persists across app restarts so the same account can
be used for later setup. Disconnect deletes the saved session and this profile
after the owned Chrome process exits. While the inspection window is open,
Chrome's inspection port listens on loopback; other local processes can reach
that port. Close the setup window when finished and
keep untrusted local programs away from the signed-in account.

### Action dispatch (format 2)

A format-2 action may declare host scheduling metadata. For example:

```json
"dispatch": {"mode": "latest", "key_arguments": ["object_id"],
             "min_interval_ms": 500}
```

`mode: "ordered"` sends fixed commands in order and omits `min_interval_ms`.
`mode: "latest"` replaces a still-pending continuous MIDI value for the same
instance, action and target; the target is formed from the named required
scalar input fields. A fixed call to a latest action, including a macro step,
is ordered. An ordered command for a target drops older pending continuous
input for that target. The host allows one dispatched command in flight per
instance and bounds both ordered and latest queues. Pending input is dropped
when it gets stale, its mapping changes, or the integration generation ends.
Replacing pending input is neither a reported success nor a user-facing error.
The action deadline begins when it is dispatched. The manifest's `completion`
text describes what a successful provider result establishes; the host shows
that text for scheduled actions instead of the generic confirmation message.

### How MIDImap presents your package

At import, users review your `name`, `author`, `version`, `description`,
package ID, archive SHA-256 and size. They also see each action's `name` and
`description`, and each permission's `description`, scope and whether it is
required to start. Write these for a nontechnical reader. Under each
`secret.read` permission MIDImap adds its own warning: the program receives
the saved value, could send it elsewhere, and turning the permission off cannot
erase a copy it already has. The setup form is
built from `config_schema` with the same input rules as action inputs below,
followed by a masked entry for each secret field.
Users may save it incomplete, so your worker only ever starts with a
configuration that validates in full.

### How the mapping editor presents an action

MIDImap builds an action's form from its manifest; packages supply no UI code.
The action `name` and the instance label identify it, and its `completion`
sentence is shown verbatim as "Completes when: …", so state precisely what
success confirms. Each top-level `input_schema` property becomes one input,
labelled with its `title` (else its name) and described by its `description`:

- `enum`, `boolean` and `null` become a menu (`boolean` shows Yes/No). An
  optional menu also offers "(not set)", which omits the field; it is never
  the same as a `null` value or an enum member with that text;
- `string`, `integer` and `number` become text fields, parsed by type; nested
  objects and arrays are entered as JSON. An optional text field has an
  Include checkbox: unticked omits it, ticked sends it, even an empty or
  whitespace string;
- a top-level `string` named `object_id` becomes a target picker of your
  discovered objects whose `capabilities` list this action. Objects that list
  no capabilities are not filtered. The picker lists targets by name, 50 at a
  time with Load more, and searches all of them, so every discovered target
  is reachable. While it is open MIDImap keeps its collections current: an
  `objects.changed` you publish updates it (renamed labels change in place),
  and it says when the list is being refreshed or may be out of date. A saved
  target that disappears stays selected, marked unavailable under its last
  known name; MIDImap never picks another. Below the picker the selected
  target's current state is shown, marked "(not current)" once it is stale.

Selections are kept by object ID or value, never by display text. Duplicate
labels get the ID or value appended, so give objects distinct labels where you
can. An input the user leaves unchanged is saved back exactly as stored.

Every call also receives a read-only `midi` object describing the trigger:
`type`, `channel`, `note`, `velocity`, `control`, `value`, `pitch` and encoder
`delta`, when present. Treat it as optional input. MIDImap never retries a call
whose outcome is unknown.

## Author walkthrough

1. Define the objects and actions your provider really supports. Use its official
   API documentation. Document rate limits, authentication requirements, stable
   identity guarantees, and what a successful call actually proves.
2. Start from the simulator example in `sdk/python/examples/simulator/`. Keep
   provider code in your `Provider`; the SDK owns framing and host-service calls.
   Do not import MIDImap application modules into a worker.
3. Define schemas and permission requirements in the manifest. Validate sample
   arguments and results before implementing network calls. Changes to action IDs
   or required fields need a compatibility decision, not silent substitution.
4. Implement initialization with bounded authentication/discovery and one owned
   connection reader where needed. Return Ready only after required access is
   confirmed. Starting a socket task is not successful initialization.
5. Implement each action with typed inputs, provider correlation, bounded deadlines,
   and a result matching its schema. An unknown object returns `TARGET_MISSING`.
   Never return success when the operation has merely entered a local queue.
6. Publish state revisions and events through the SDK. Retain stable IDs on rename,
   expose unavailable devices honestly, and respect host limits. Do not busy-poll
   or start a new listener for every macro invocation.
7. Use SDK host services for supported networking, credentials, and authentication.
   Handle denied grants as a normal domain error. Background operations require
   the same grants as actions. Do not request unrelated scopes “for later.”
8. Implement shutdown: stop admission, cancel owned background tasks, close
   connections, and await cleanup within the host grace period. Do not leave
   detached child processes or write persistent state into the package directory.
9. Run SDK conformance cases: invalid inputs, bad results, expired auth, missing
   targets, disconnect while executing, timeout, repeated startup/shutdown, and
   permission revocation. Test simultaneous responses and unsolicited events.
10. Build with the pinned recipe below. Inspect the artifact for credentials and
    caches, record its digest, and test that exact archive. The developer
    [supervisor harness](integration-processes.md) can run the simulator; the
    application package UI is not available yet.

## Python SDK

Install the SDK into your development environment from this repository with
`python -m pip install --require-hashes -r sdk/python/requirements.txt`, then
`python -m pip install --no-deps ./sdk/python`. The import name is
`midimap_integration_sdk`; it needs Python 3.12 or later and nothing from the
MIDImap application. Importing it starts nothing.

Put the provider in its own module, `lamp_provider.py`:

```python
from midimap_integration_sdk import (
    ErrorCode, IntegrationError, ObjectInfo, Page, Provider, Setup, State, action,
)

class LampProvider(Provider):
    async def initialize(self, setup: Setup) -> None:
        self.setup = setup            # finish required setup before returning

    async def list_objects(self, collection_id, cursor):
        return Page([ObjectInfo("lamp-1", "devices", "Demo lamp", None, ("set_level",))])

    async def read_states(self, object_ids):
        return [State("lamp-1", "devices", 0, {"level": 0})]

    @action("set_level")
    async def set_level(self, arguments):
        if not await self.setup.host.probe("simulator.probe"):
            raise IntegrationError(ErrorCode.PERMISSION_DENIED, "The probe is not allowed.")
        return {"object_id": arguments["object_id"], "level": arguments["level"]}
```

and start it from the entry script, `worker.py`, which imports only the SDK:

```python
import sys
from midimap_integration_sdk import package_manifest, run_worker

def create_provider():
    from lamp_provider import LampProvider    # imported after stdout is claimed
    return LampProvider()

if __name__ == "__main__":
    sys.exit(run_worker(create_provider, package_manifest(__file__)))
```

`run_worker` claims the protocol streams first and only then calls the factory,
so nothing your provider module prints while importing or constructing (even
native writes to the standard output descriptor) can corrupt the protocol.
`package_manifest(__file__)` finds `manifest.json` both from source and in the
frozen worker.

- **Actions:** one `@action("<id>")` coroutine method per manifest action; the
  runtime refuses to start if any is missing or undeclared. Methods receive
  validated arguments and must return a value matching `output_schema`;
  anything else is reported as `PROVIDER_ERROR` and never sent.
- **Errors:** raise `IntegrationError(code, message)` with a fixed sentence you
  wrote. Host-only codes are refused. Any other exception becomes
  `PROVIDER_ERROR` "The integration failed."; its text is never sent or logged.
- **Initialization:** MIDImap reports Ready only after `initialize` returns. The
  SDK first checks the package ID and version, protocol version, configuration
  (against `config_schema`) and grants, and answers `NEEDS_SETUP` when a
  permission required for initialization is not granted.
- **Secrets:** `await setup.host.read_secret("server_password")` returns the
  saved text. It raises `HostCallError` with `PERMISSION_DENIED` (not granted,
  or not listed by the calling action), `NEEDS_SETUP` (nothing saved, or
  unreadable) or `INSTANCE_UNAVAILABLE`. When that error is retryable, the
  saved value changed while it was being read, and reading again is safe. You may read during `initialize`.
  Once returned, the value is in your hands: never log it, put it in results,
  errors, events, labels or state, or store it outside memory. MIDImap cannot
  filter what an integration does with a value it was given, and its own logs
  record integration failures only as fixed text and codes.
- **Sign-in:** `token = await setup.host.access_token("account")` returns an
  `AccessToken` with `value`, `expires_at` (epoch seconds or None) and
  `scopes`. It raises `HostCallError` with `PERMISSION_DENIED`, `NEEDS_SETUP`
  (not signed in, expired, or the sign-in could not be renewed safely),
  `INSTANCE_UNAVAILABLE` (try again later when retryable) or `SATURATED`.
  A renewal may take a few seconds, so give the call enough `timeout_s`. Treat
  the token like a secret value: never log it or put it anywhere persistent.
- **Host calls:** `setup.host.probe(...)` and `read_secret(...)` are linked
  automatically to the request being handled, and their deadline never exceeds
  that request's remaining time.
  Work that runs on its own or outlives its request must be started with
  `setup.background.start(work)`; such calls are background calls, accepted
  only while Ready. A task that inherits a finished request's context raises
  `ContextError` instead of borrowing it. A call that times out locally raises
  `HostCallTimeout`, but its capacity is held until MIDImap answers.
- **Events:** `setup.events.publish_state(State(...))`,
  `publish_event(object_id, event_id, payload)` and
  `report_unavailable(code, message)`. You own each object's `revision`: within
  a worker session it may repeat (a refresh) or increase, never decrease, and
  an object keeps its collection. The same rule covers published state and
  `read_states` results, so they never disagree; violations raise `ValueError`
  when publishing and become `PROVIDER_ERROR` for a snapshot (none of that
  snapshot is recorded). An equal revision must carry identical `values` and
  `available`; change the revision whenever the state changes. A session
  tracks at most 5000 objects and never
  forgets one: a new object beyond that raises `TooManyObjects` (`SATURATED`)
  while known objects stay updatable. The SDK assigns sequence numbers when
  sending. Waiting state for the same object is replaced, occurrences never
  are; a full queue raises `EventQueueFull`. Publishing is refused before
  MIDImap receives the initialization result and after shutdown begins.
- **State freshness:** a cached state is current for its collection's
  `state_ttl_seconds` after it was observed. While something shows an
  object's state, MIDImap calls `read_states` for it at half its TTL (in
  batches of at most 100), and at once when a new view starts showing it. An
  answer at an equal revision with identical content renews the TTL; nothing
  else does: republishing the same state, or a newer answer to an older
  request, never renews it, and an answer cannot restore freshness after a
  disconnect. Never invent revisions to keep state fresh. Answer
  `read_states` only from a model that is complete and current (connected,
  loaded, no event lost); otherwise raise a retryable `INSTANCE_UNAVAILABLE`.
  A state nobody shows is never polled and simply expires.
- **Discovery changes:** call `setup.events.publish_objects_changed(
  "<collection id>")` whenever objects of that collection are added, removed
  or renamed (and when your whole catalog is replaced). It is an
  invalidation, not a list: notices still waiting for one collection are
  merged, so call it for every change. When something shows that collection,
  MIDImap waits 100 ms, then reads it again completely with `list_objects`
  (pages of at most 100, at most 5000 objects and 100 pages) and replaces the
  old list only when that read completes with no newer change; a failed or
  overtaken read removes nothing. A repeated cursor, too many pages or
  objects, or an object listed twice is refused and not retried until the
  collection changes again. If your listing spans pages, a change between
  pages must not make it skip or repeat an object: put a catalog version in
  your cursors and fail the next page (retryable `INSTANCE_UNAVAILABLE`) when
  the version moved, or serve every page from one snapshot.
- **Deadlines and shutdown:** a handler still running at its deadline is
  cancelled; if it finishes anyway, its validated result is still sent. On
  shutdown, the SDK stops admission, cancels handlers and background work, runs
  `Provider.shutdown()`, and answers within the shutdown budget. Work that
  ignores cancellation is reported as cancelled, never as success, and an
  incomplete cleanup is reported as an error. One deadline (the shutdown
  request's) covers both cleanup and writing the final answer: if MIDImap stops
  reading, the worker still exits unclean on time. MIDImap's supervisor ends
  the process tree in any case.
- **Streams:** `run_worker` claims stdin and stdout for the protocol before it
  calls your factory; `print()`, logging and native writes to the standard
  output descriptor go to stderr afterwards. Keep provider imports inside the
  factory, as above.

Packages remain trusted executable code: the SDK validates its own protocol
traffic but is not a sandbox, and MIDImap checks every host-service call
against the user's grants regardless of what the SDK reports.

## The worker's environment

MIDImap starts your worker with an intentionally minimal environment and
inherits nothing from the app: only `SystemRoot`, `WINDIR`, a `PATH` containing
the Windows System32 directory, `TEMP` and `TMP`, and two Python settings. In
particular there is no `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA`,
`LOCALAPPDATA` or `USERNAME`, so these fail or misbehave inside a worker:

- `pathlib.Path.home()` raises `RuntimeError`;
- `os.path.expanduser("~")` returns `"~"` unchanged, so a library writing to
  `~/.cache` writes beneath a literal `~` folder instead;
- `getpass.getuser()` fails.

`TEMP`/`TMP` point at a private scratch directory for this run only. Use it for
transient files; it is not durable configuration and not a place for secrets.
Configure every library cache or data directory explicitly (to that scratch
directory, or to nothing) instead of relying on home or profile defaults, and do
not store credentials in files. The worker's working directory is the same
scratch directory. Never write into the package directory: installed content
must stay exactly as verified.

## Building a package

The recipe produces a one-directory frozen worker for Windows x64 with
PyInstaller 6.22.3 (GPL-2.0-or-later with an exception that allows distributing
built applications) and CPython 3.12. All build inputs are exact versions with
SHA-256 hashes in `sdk/python/requirements-build.txt`. Build in a separate
virtual environment, never your system Python, from the repository root:

```powershell
python -m venv build\integration-sdk\builder-venv
build\integration-sdk\builder-venv\Scripts\python.exe -m pip install --require-hashes -r sdk\python\requirements-build.txt
build\integration-sdk\builder-venv\Scripts\python.exe -m pip install --no-deps --force-reinstall .\sdk\python
build\integration-sdk\builder-venv\Scripts\python.exe sdk\python\tools\build_package.py --source sdk\python\examples\simulator --out build\integration-sdk
```

Repeat the SDK installation (`--force-reinstall`) after every SDK change:
the build freezes the SDK installed in the builder environment, not the source
tree.

`--source` is your package directory: `manifest.json`, `README.md`, `LICENSE`,
and the entry script (`worker.py` unless `--entry` says otherwise) with the
modules it imports. The tool writes:

- `worker/` named after the manifest `entrypoint`, containing the frozen worker
  and its own Python runtime;
- `worker/licenses/` with the license texts of Python and of each runtime
  dependency;
- `<id>-<version>.midimap-integration`, a ZIP with sorted entries and fixed
  timestamps;
- its `.sha256` digest and `build-report.json`.

The tool never runs the worker. Users need no Python, and the worker uses
nothing from the MIDImap installation.

Run the SDK conformance suite from the repository root with
`python -m unittest src.tests.test_integrations_sdk`. It checks the SDK against
the host protocol engine, handwritten host frames and the golden message
fixtures.

## Minimal simulator behavior

The manifest above has object `lamp-1` in collection `devices`, label `Demo lamp`,
initial level 0, and no parent. `set_level` invokes the host probe, applies the
level only if allowed, increments its revision, emits `state.changed` and
`level_changed`, and returns the confirmed result. Both discovery and the initial
state are available without the optional probe grant.

Fault modes (immediate success, delay, failure, nonresponse, crash, invalid
result, malformed frame, event flood, and attempted child persistence) live
only in the test harness, in a separate fixture package whose manifest declares
them (`src/tests/fixtures/integrations/simulator_faults.manifest.json`). The
simulator itself has no fault modes. Never hide fault injection in a real
provider package, and declare every test-only action in its fixture manifest.

## Worker protocol reference (draft v1)

The host and worker exchange UTF-8 JSON objects, one per LF-terminated line, over
the worker's redirected stdin/stdout. CRLF is accepted; LF is emitted. A BOM,
invalid UTF-8, duplicate keys, NaN/Infinity, trailing data, unknown envelope
fields, and unknown methods are rejected. Stdout carries protocol only; author
logging goes to stderr. This is a MIDImap protocol, not MCP. It remains a draft
until the simulator and OBS reference have exercised it.

| Kind | Fields besides `v: 1` and `kind` | Rules |
|---|---|---|
| `request` | `id`, `method`, `timeout_ms`, `params` | `h-<n>` from the host, `w-<n>` from the worker; timeout is a positive bounded integer; params is an object |
| `response` success | `id`, `ok: true`, `result` | Exactly one terminal response; result validated per method or action |
| `response` error | `id`, `ok: false`, `error` | `code`, `message`, `retryable`, `outcome_unknown`; no result |
| `event` | `name`, `seq`, `data` | Worker only; `seq` starts at 1 and increases by one per session |

Each side numbers its own requests: `<n>` is a positive decimal number without
leading zeros that increases with every request it sends in a session (gaps
are allowed, reuse is not). The host rejects a worker request whose number does
not increase. Responses may arrive in any order; each must answer a request
that is still pending, exactly once. `timeout_ms` is the time remaining when
the request is written, measured on the sender's monotonic clock.

Host-to-worker methods are `session.initialize`, `action.invoke`,
`discovery.list`, `state.snapshot`, and `session.shutdown`. Worker-to-host
methods are the test-harness `host.test.probe`, `host.secret.read` and
`host.auth.token`.
Events are `state.changed`, `object.event`, `objects.changed` and
`instance.unavailable`. Events are accepted only after initialization
succeeds.

Invoking the simulator's action, and its success and failure responses:

```json
{"v":1,"kind":"request","id":"h-2","method":"action.invoke","timeout_ms":30000,"params":{"action_id":"set_level","arguments":{"object_id":"lamp-1","level":25},"midi":{"value":64}}}
```

```json
{"v":1,"kind":"response","id":"h-2","ok":true,"result":{"object_id":"lamp-1","level":25}}
```

```json
{"v":1,"kind":"response","id":"h-2","ok":false,"error":{"code":"TARGET_MISSING","message":"The selected object is unavailable.","retryable":false,"outcome_unknown":false}}
```

A discovery page and a state update:

```json
{"v":1,"kind":"response","id":"h-3","ok":true,"result":{"objects":[{"id":"lamp-1","collection_id":"devices","label":"Demo lamp","parent_id":null,"capabilities":["set_level"]}],"next_cursor":null}}
```

```json
{"v":1,"kind":"event","name":"state.changed","seq":1,"data":{"object_id":"lamp-1","revision":1,"values":{"level":25},"available":true}}
```

Objects of a declared collection were added, removed or renamed. This is an
invalidation only, never a list of objects; MIDImap rescans the collection when
something shows it:

```json
{"v":1,"kind":"event","name":"objects.changed","seq":2,"data":{"collection_id":"devices"}}
```

A worker-to-host call names the host request it is serving in
`parent_request_id`:

```json
{"v":1,"kind":"request","id":"w-1","method":"host.test.probe","timeout_ms":5000,"params":{"permission_id":"simulator.probe","parent_request_id":"h-2"}}
```

The parent must be a host request that is still running and within its
deadline; the call's deadline is the smallest of its own `timeout_ms`, 30
seconds, and the parent's remaining time. Use `null` for a background call,
which is only accepted once the instance is ready and gets just its own bounded
deadline. An unknown, finished, or expired parent is answered with an error
before any host service runs. The parent only links deadlines; it is never a
permission grant.

MIDImap decides every host call itself, from the permissions the user
granted to this instance and its exact package version; the snapshot your
worker received is only a hint. `host.test.probe` answers `{"allowed": true}`
only after its protected work ran. Every refusal is an error response:
`PERMISSION_DENIED` when the permission is not declared, not granted, granted
for another scope, or (from an action) not listed in that action's
`permissions`; `INSTANCE_UNAVAILABLE` when the grants changed or the instance
is stopping, after which MIDImap restarts your worker with the new grants. An
error with `outcome_unknown: true` means the host work may already have taken
effect. Host-side details are in
[integration host services](integration-host-services.md).

`host.secret.read` takes `{"field_id": "<id>", "parent_request_id": ...}` and
answers `{"value": "<text>"}`. MIDImap checks it when it is admitted and
again just before answering, so a value read while the call was allowed is
not sent if, meanwhile, the permission was revoked, the worker is stopping,
the parent request finished or a deadline passed. Refusals use fixed codes:
`PERMISSION_DENIED`, `NEEDS_SETUP` (not saved or unreadable),
`INSTANCE_UNAVAILABLE`, `TIMEOUT` or `SATURATED`. If a required field is found
missing while running, MIDImap answers `NEEDS_SETUP`, stops that worker and
shows the instance as Needs setup.

`host.auth.token` takes `{"auth_id": "<id>", "parent_request_id": ...}` and
answers `{"access_token": "<text>", "token_type": "Bearer", "expires_at":
<int|null>, "scopes": [...]}`, with the same two checks, fixed refusal codes
and Needs setup behavior. A call may wait for a renewal in progress.

The host counts a request as finished only when the worker's single response
to it has arrived and every host call made for it has actually completed; a
host call answered with `TIMEOUT` still counts until its work stops. Until
then the request keeps its capacity. After responding to a request, a worker
cannot start new calls for it, and a second response to it is a protocol error.
A worker may still return an error for a request after one of its host calls
timed out. Deadlines are measured by elapsed time, so a response that arrives
after its request's deadline never turns the timeout into a success.

Workers may report `INVALID_ARGUMENT`, `PERMISSION_DENIED`, `NEEDS_SETUP`,
`INSTANCE_UNAVAILABLE`, `TARGET_MISSING`, `SATURATED`, `TIMEOUT`, `CANCELLED`,
`WORKER_EXITED`, `PROTOCOL_ERROR`, and `PROVIDER_ERROR`. `PACKAGE_CONFLICT`,
`PACKAGE_INVALID`, `PACKAGE_MISSING`, `STORE_ERROR`, and `INCOMPATIBLE_VERSION`
are host-only. `message` is a short user-safe sentence, never an exception
dump. `retryable` never permits automatic replay, and `outcome_unknown` marks an
external effect that may already have happened.

The `session.initialize` grants snapshot has the shape
`{"revision": 3, "decisions": [{"permission_id": "simulator.probe", "scope": {"resource": "simulator"}, "decision": "granted"}]}`;
absent permissions are denied.

Key limits, defined in `src/integrations/policy.py`:

| Resource | Limit |
|---|---|
| Archive compressed / expanded size | 256 MiB / 512 MiB |
| Archive entries / single expanded file | 4096 / 256 MiB |
| Archive entry path / path segment | 512 / 255 characters |
| Manifest / message including newline | 256 KiB / 1 MiB |
| JSON nesting, including expanded schema references | 32 levels |
| Identifiers / error text | 128 / 512 characters |
| Names and labels / descriptions / discovery cursor | 128 / 1024 / 1024 characters |
| Discovery page / cached objects per instance | 100 / 5000 |
| Action timeout | 30 seconds default, 1–300 seconds |
| Host-service request timeout | At most 30 seconds |
| Startup / discovery and state requests | 15 / 10 seconds |
| State freshness TTL per collection | 1–3600 seconds, default 30 |
| Discovery rescan after `objects.changed` | after 100 ms; repeated rescans paced up to 5 seconds |
| One complete discovery scan | at most 100 pages and 5000 objects |
| Host-started scans and state refreshes in flight | 1 per instance, 4 for all instances |
| State refresh | at half TTL, batches of up to 100 taking objects due within 5 seconds; unanswered objects retried after 1, 2, 4 ... 30 seconds |
| Open discovery/state views (interests) | 64 per MIDImap process, up to 100 objects each |
| Worker: running actions / other requests / host calls | 8 / 8 / 8 at once |
| Worker: background tasks / waiting events | 8 / 256 |
| Worker: objects tracked per session (state revisions) | 5000 |
| Worker: shutdown answer / stderr log record | 1 second before the deadline / 1024 characters |
| Secret fields per package / secret value | 8 / 2048 UTF-8 bytes |

The overlay v1 contract is being developed separately; these policy limits do
not by themselves enable overlay manifests or host services:

| Overlay resource | Limit |
|---|---|
| Toast specification / elements / element depth / text | 16 KiB / 64 / 6 / 256 characters |
| Menu specification or page fragment / items per page / elements per item subtree | 64 KiB / 200 / 8 |
| Menu elements per spec / cursor / lazy total | 1,665 / 1,024 characters / 1,000,000 items |
| Menu additional pages / slider intervals per increment | 32 / 10,000 |
| QR payload | 2,048 printable ASCII characters |
| Runtime patches / live toasts per instance | 30 per second per toast / 4 |
| PNG asset bytes / dimensions / assets per instance | 256 KiB / 512×512 / 32 |
| Menu items per page / sparkline points | 200 / 64 |
| Duration, linger, bounded sticky duration, or countdown | 600,000 ms |


Format-3 overlay requests:

```json
{"v":1,"kind":"request","id":"w-11","method":"host.ui.toast","timeout_ms":5000,"params":{"permission_id":"overlay.toast","parent_request_id":null,"spec":{"v":1,"toast_id":"demo","size":"standard","root":{"type":"text","text":"Ready"}}}}
```

```json
{"v":1,"kind":"request","id":"h-11","method":"ui.menu.event","timeout_ms":5000,"params":{"menu_id":"controls","event":{"kind":"select","item":"ping"}}}
```


## First real reference: OBS

### Using the managed WebSocket service

Declare one connection and a matching permission; the host resolves its address
from the instance's ordinary settings before asking for consent:

```json
{
  "connections": [{"id": "peer", "transport": "websocket", "scheme": "ws",
    "host_config": "host", "port_config": "port", "path": "/",
    "subprotocols": []}]
}
```

This is a manifest fragment: the settings schema must declare `host` as a
string and `port` as an integer, and the permissions must include a
`network.websocket` permission scoped to `{"connection": "peer"}`. Actions
that send through it must list that permission. Require it for initialization
when startup needs the connection. Authentication uses its own secret/token
permission; do not put credentials in the URL or ordinary settings.

In `initialize`, use `self.connection = await setup.host.open_websocket("peer")`.
Start the reader with `setup.background.start(self.read_messages)`; it can run
during initialize and continue into Ready. `await self.connection.receive()`
returns one text message and handles empty waits internally. Catch
`WebSocketClosed` to inspect its numeric `code`; remote reason text is omitted.
Only one reader can wait at a time. A cancelled reader leaves its pending
response available to a subsequent reader.

Use `await self.connection.send(text)` in the action's context so the host can
check its declared permission and deadline. Send does not mean the remote
application finished: correlate and await the provider's response separately.
Never resend a request whose result is uncertain. `await self.connection.close()`
closes explicitly; host teardown also owns closure. A maximum 128 KiB text
message is supported; binary messages are not. Do not log provider frames,
credentials or raw remote errors. See `integration-host-services.md` for limits.

The OBS development worker now uses this transport; see the
[OBS README](../sdk/python/examples/obs/README.md) for build and service-run
commands. Target: OBS 32.0.2 / obs-websocket 5.6.3, RPC 1. Simulator qualification
does not establish real OBS or packaged-app support. Opt into host-managed recovery with top-level
`"reconnect": {"enabled": true}`. Missing/false preserves manual recovery.
Use an `IntegrationError(..., retryable=True)` during initialize, or
`setup.events.report_unavailable("INSTANCE_UNAVAILABLE", "Connection lost.", retryable=True)`
after Ready, only for explicitly classified transient connection failures.
Authentication, secret, permission and protocol failures require correction and
manual Retry. Unknown defects must not be retried silently. The host retires all
old resources, schedules one jittered backoff attempt and creates a fresh worker;
no actions are replayed. Retry replaces a timer. Live discovery
(`objects.changed`) and state freshness follow "Discovery changes" and "State
freshness" above (step 3b); after reconnection the host rediscovers watched
collections and refreshes watched state in the new generation.

`publish_state` during initialize stages validated state in the SDK's bounded
outbox. Successful initialize promotes it after the success response; failure
discards it. An unavailability report before Ready fails initialize directly.
Occurrence events still require Ready. Newly discovered IDs still need normal
host discovery and snapshot before cache state is visible. An `objects.changed`
published during initialize is staged the same way. Do not retry refused setup
publication opportunistically.

After the foundation, implement an OBS package with actual authentication,
scene/input discovery, scene change, and input mute. The package owns provider
message correlation and maps provider objects to the shared discovery model.
The host owns approved endpoints, credential handles, and managed transport
resources. Reconnect restores subscriptions and snapshots; it does not replay
an action with unknown external outcome.

OBS protocol research from the old prototype can help, but recheck it against
the provider's current protocol and exercise it with a real installation. Studio
One MIDI examples and smart-home aspirations are not evidence of supported
packages.

## Sharing and troubleshooting

Share a package to distribute executable integration capability. Share an
automation bundle to distribute macro ideas that require configured integrations.
These are distinct artifacts. The latter's import wizard binds or configures
local instances; neither artifact exports tokens or grants.

Logs should identify the instance, action, request, error code, and duration.
Never print full provider frames, authorization headers, raw exceptions, or
private user state. If an action times out, report that it may have reached the
provider; do not tell the user to retry a non-idempotent operation blindly.


## Overlay UI

Format 3 supports permission-scoped toast and menu overlays, action feedback templates, and MIDI menu controls. See [Overlay UI v1](overlay-ui.md) and `sdk/python/examples/simulator_overlay/`. Formats 1 and 2 retain their existing validation and behavior.
