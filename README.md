# MIDImap integrations

Public MIT-licensed sources for the curated development integrations and their Python SDK.
Browse/download reviewed releases from the Integration Store inside MIDImap.

| Integration | Development version | Qualification |
| --- | --- | --- |
| [OBS Studio](integrations/obs) | 0.1.1 | Windows 11 AppContainer worker; simulated OBS peer, including password/reconnect |
| [Google Home](integrations/google_home) | 0.1.8 | Windows 11 AppContainer worker; synthetic signed-RPC peer |

Real-provider/account/Chrome, packaged MIDImap and Windows 10 qualification are
not established by those checks. Google Home uses a private protocol that can change.
Exact archive digests and coverage notes are in the public catalog:
https://midimap-updates.hebopavlos.workers.dev/integrations/catalog

## Build

Use Windows x64 Python 3.12 in an isolated builder environment. Install the
hash-locked sdk/requirements-build.txt, then install ./sdk without dependencies
or build isolation. Run sdk/tools/build_package.py with --source integrations/obs
(or integrations/google_home) and --out build/obs. Read [the author guide](docs/author-guide.md).

## Contribute

Issues and pull requests are welcome. Explain each permission and endpoint in
the manifest, declare minimum MIDImap compatibility and an icon, and preserve
action/object IDs. Include synthetic tests and precise real-provider coverage.
Never commit credentials, browser profiles, personal devices or user configuration.
Pull requests do not publish packages: maintainer review and exact-digest
qualification precede curated store publication. There is no automatic install.

This dedicated repository contains no private MIDImap application state or credentials.
