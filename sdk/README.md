# midimap-integration-sdk

Python SDK for writing MIDImap integration packages (draft worker protocol v1).
Import name: `midimap_integration_sdk`. The author guide in the MIDImap
repository (`docs/integration-packs.md`) documents the API, the package format
and the build recipe; `examples/simulator/` is a complete package.

The SDK talks to MIDImap over the worker's standard streams, validates every
argument, result and event against the package manifest, and keeps deadlines
and capacity limits. Packages are trusted executable code: the SDK is not a
sandbox, and MIDImap permissions control MIDImap-owned services only.

Managed connections use `await setup.host.open_websocket(connection_id)` and
the returned object's async `send(text)`, `receive()` and `close()` methods.
Declare the connection and its `network.websocket` permission in the manifest.
The host owns networking and its dependency; workers need no WebSocket library.
See the author guide's managed-WebSocket section for reader lifetime, limits
and uncertain-send handling. The OBS development example now includes a worker
and build/service-run instructions in `examples/obs/README.md`. Its target is
OBS 32.0.2 / obs-websocket 5.6.3, RPC 1. Source tests, built-worker tests, real OBS,
packaged MIDImap and release qualification are separate gates. Host-managed reconnect is available only with manifest `reconnect: {"enabled": true}`
and an explicitly retryable connection lifecycle failure. Absent means false.
Use `setup.events.report_unavailable(code, message, retryable=True)` for a known
transient connection failure; never mark authentication, permission or protocol
errors retryable. The host starts a fresh worker only after resource retirement,
never replays actions, and shows the next attempt.

Call `setup.events.publish_objects_changed(collection_id)` when objects are
added, removed or renamed; MIDImap rescans that collection completely when
something shows it. An equal state revision must carry identical content.
MIDImap renews an unchanged state's freshness only from your `read_states`
answer at an equal revision, so never invent revisions, and answer only from
a current model. See the author guide, "Discovery changes" and "State
freshness".

State publication during initialize is staged in the bounded SDK outbox and
promoted only after the success reply. Failure discards it; an unavailability
report before Ready fails initialize. Occurrence events still require Ready.

`src/midimap_integration_sdk/_wire/` is a byte-identical copy of the host's
wire-contract modules; refresh it with `tools/sync_wire.py`, never edit it.
