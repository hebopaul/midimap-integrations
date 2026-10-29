# OBS Studio development reference package

C5 steps 2 / 3a: a Windows x64 SDK worker using MIDImap's host WebSocket broker.
The initial qualification target is OBS **32.0.2 / obs-websocket 5.6.3, RPC 1**.
Evidence is recorded in `supervision/023_result.md` (step 3a) and the preserved step-2 review; real OBS, packaged MIDImap
and release qualification are separate gates. Missing required UUID/audio
features fail explicitly. Older versions are not claimed as tested support.

## Setup and behavior

Import the development package, create an OBS instance, enter the OBS computer
(127.0.0.1 for this PC) and server port (normally 4455), and approve the displayed
exact endpoint. The declaration uses `/` and `obswebsocket.json`. Plain
WebSocket consent explains that it is unencrypted; the host permits only
loopback/private destinations. Changed endpoints require their own grant.
Enter the password using MIDImap's separate secret field/permission, never
ordinary settings or a URL. It is read only if OBS's Hello requests authentication.

Ready follows authentication, scene/audio discovery and all required mute reads.
Nonaudio inputs are excluded. Both actions await OBS's response:

- **Switch scene:** OBS accepted the program-scene switch and started its
  transition; the transition may still be running. Studio mode changes program.
- **Set input mute:** sets the explicit Boolean value, never toggles.

UUIDs preserve mapping identity across renames. Requests use UUID-only targets;
deleted targets fail instead of selecting another object. The provider maintains
its internal catalog from events. Every addition, removal and rename of a
listed scene or audio input, and every scene collection change, publishes
`objects.changed`, so open pickers update. Listing cursors carry the catalog
version: a change between two pages makes the next page fail (retryable)
rather than skip or repeat an object, and MIDImap rescans.

Returning to a previously loaded scene collection refreshes every present audio
input's mute state, including retained UUIDs. Actions and state queries wait
until that collection's required snapshot is complete. Newer live events win
over snapshot replies; replies from superseded collections/loads are ignored.

Disconnect makes state stale/unavailable. A submitted action without a definitive
reply has an uncertain outcome and is never replayed. This manifest opts into
host-managed reconnection for explicitly classified transient connection failures.
The host retires the old worker/resources, waits with jittered backoff, and
starts a fresh worker that authenticates and reloads its snapshot before Ready.
Authentication, secret, permission and protocol failures require correction and
manual Retry. OBS operator Kick (4011) never reconnects automatically. Retry
replaces a pending timer; disable, removal and settings/permission changes cancel
it. The worker contains no reconnect loop.
`read_states` answers from the event-maintained model only while it is
connected, completely loaded and not being replaced. Unchanged state keeps its
revision, so MIDImap renews freshness at half the one-hour TTL for objects it
shows, without fabricated revisions.

The host bounds text messages to **128 KiB UTF-8**, 16 queued messages and 1 MiB
queue bytes. The package bounds pending OBS requests to 32, load-time events to
256 / 1 MiB and known identities to 4000. SDK publication has a 256-object waiting
bound, including staged initial state: an oversized initial collection fails
explicitly. No truncation or silent event loss is supported.

## Build and verify

Use Windows x64 CPython 3.12 and a separate builder environment. From the repo:

```powershell
.venv/Scripts/python.exe -m venv build/integration-reconnect-023/builder-venv
build/integration-reconnect-023/builder-venv/Scripts/python.exe -m pip install --require-hashes -r sdk/python/requirements-build.txt
build/integration-reconnect-023/builder-venv/Scripts/python.exe -m pip install --no-build-isolation --no-deps ./sdk/python
build/integration-reconnect-023/builder-venv/Scripts/python.exe -B sdk/python/tools/build_package.py --source sdk/python/examples/obs --out build/integration-reconnect-023
.venv/Scripts/python.exe -B -m unittest src.tests.test_integration_obs src.tests.test_integration_obs_broker src.tests.test_integration_obs_cleanup src.tests.test_integrations_reconnect src.tests.test_integration_obs_reconnect
.venv/Scripts/python.exe -B -m src.tests._smoke_obs_reconnect --package build/integration-reconnect-023/org.midimap.obs-0.1.0.midimap-integration --out build/integration-reconnect-023/package-check
```

Offline builds may add `--no-index --find-links build/integration-sdk/wheels`
to the hash-locked dependency install. Reinstall the SDK after SDK changes.
The existing builder freezes new source and writes fixed-timestamp, sorted ZIP
entries, licenses and `build-report.json`. Record actual package/worker hashes;
ZIP ordering does not promise bit-identical PyInstaller binaries. The worker
bundles no WebSocket client: the host supplies websockets==16.0.

The smoke command imports into a temporary repository and runs the actual worker
through the real service and a loopback-only synthetic OBS simulator. It uses a
unique synthetic Credential Manager namespace and verifies deletion. It opens
no GUI, MIDI or physical audio/video devices and never streams or records.
Use a fresh output directory for each run. The separate real-OBS guest harness
records cleanup phases independently, including secret absence, MIDImap resource
retirement, external OBS graceful/forced exit, process absence and diagnostic
canary scans. A failed OBS graceful exit cannot skip the remaining audits.

The current host runs packaged workers in capability-free AppContainers, with
networking and credential access through permission-checked host services. The
packaged-worker check now verifies that isolation as well as reconnect behavior;
see `docs/integration-security-rollout.md` for current evidence and remaining
release gates. Real OBS must use the approved network-disabled
Sandbox/disconnected VM. The accepted step-2 candidate remains preserved under `build/integration-obs-022-review/`.
The real OBS run's failed overall result is preserved; exit diagnosis remains
a separate C5 gate. C5 acceptance, the one-hour automated soak, broader
platform gates and the deferred C4.4 walkthrough remain open.
