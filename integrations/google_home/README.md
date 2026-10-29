# Google Home development package

This package uses a host-owned Google Home browser session. The worker receives
only a non-secret session status and the parsed responses of fixed Google
Home operations. Cookies, request signing, Chrome inspection and HTTPS live in
MIDImap. The integration uses Google's **private web protocol**, which can
change without notice.

## Current implementation

- A format 3 manifest declares one Google Home session and collections for
  lights, rooms, automations and thermostats. The overlay permission enables
  the MIDI menus.
- The provider discovers lights in the selected home, keeps stable IDs across
  renames, filters query-only traits when their access metadata is understood,
  reads current state and reports offline status. Unrecognized per-device
  access metadata withholds that device's write capabilities without blocking
  discovery. Catalog refresh runs every 60 seconds while the worker is ready.
- Power and brightness use the observed `UpdateTraits` request format and
  require the response to report the requested state for the exact target.
  An HTTP 200 alone is not accepted. An unreadable or mismatched response is
  reported as an unknown write outcome and is never retried automatically.
- Toggle reads the current on/off state first and refuses when that state is
  unavailable or offline. It then uses the same guarded power action. Another
  toggle waits until Google reports the preceding requested state.
- After an accepted write, a background read publishes observed state. Reads
  are coalesced to at most once per second while continuous input is active,
  followed by a final read after input goes idle.
- Version 0.1.4 also discovers automations, reports their enabled state and
  permits enabled automations to be run. Automation protocol shapes and
  acknowledgement handling are covered by synthetic tests, but have not been
  qualified against a live account.
- Version 0.1.5 keeps Google Home routine entries out of the device browser;
  routines remain available through the separate automation browser.
- Version 0.1.7 also excludes routine traits and scenes from device and light
  target lists. Its named request toasts animate pending/accepted phases and
  distinguish failure from an unknown outcome. Use a host supporting the
  `$feedback.*` presentation bindings.
- Each target field declares its collection, so automation, light, room and
  thermostat choices cannot mix. Disabled automations keep the declared run
  capability and report enabled=false; their menu entries are disabled and
  execution refuses them. No fictional capability IDs are returned.
- Light color, color temperature and room power/brightness commands use the
  same guarded acknowledgement checks. Room power state is available only
  when all its discovered lights report an online on/off state.
- Four MIDI menus browse rooms, a selected room, home devices and automations.
  Confirm opens a device's controls, edits a value, or toggles the selected
  switch. There is no confirm-behavior setting. Double press can toggle a
  device directly. Devices without supported controls are listed as unavailable.
  AC/thermostat controls merge graph and trait metadata, preserve mode IDs such
  as `fan-only`, use independent power where supported, and respect advertised
  temperature limits. Offline controls remain visible but disabled.
- The host attempts periodic cookie rotation for non-device-bound sessions
  while an instance is enabled and MIDImap is running. A revoked session or
  one bound to a browser key may still require a new sign-in.

## Clean-profile setup under development

1. Install the package in a host build that supports manifest format 3. Create
   an instance and approve its Google Home session permission.
2. Open **Connect Google Home**. MIDImap opens installed Chrome in a fresh,
   isolated per-instance profile under local application data. Sign in there;
   a sign-in in your ordinary Chrome profile is separate.
3. Open Google Home **Devices** and finish signing in. Click **Find homes** in
   MIDImap. It closes the ordinary setup window and reopens the same isolated
   profile with inspection enabled. If more than one Google Home tab is open,
   choose the right tab and click **Find homes** again. MIDImap makes an
   independent read-only `GetHomeGraph` request before showing the account and
   home choices.
4. Choose the home and click **Use this home**. The verified session is saved
   in Windows credential storage. Close the setup window when finished. Its
   loopback Chrome inspection port is reachable by other local processes after
   **Find homes**, while the reopened setup window is open.
5. Use **Disconnect** to delete the saved session and isolated profile after
   the owned Chrome process exits. Headless mode never launches Chrome.

The isolated Chrome profile persists across app restarts until Disconnect; it
contains sign-in state and must be kept private. If setup was interrupted,
reopen **Connect Google Home**. A stale `DevToolsActivePort` marker from a
crashed browser is removed automatically after its port is checked.

Version 0.1.3 normalizes the full OnOff and Brightness trait identifiers in
GetHomeGraph to the shorter names in GetTraits. A read-only live check of two
selected-home lights confirmed both declarations and access results, and the
provider now advertises their power, toggle and brightness capabilities.
Version 0.1.2 had already added nested trait-access parsing, the ordered Toggle
power action and guarded write-response validation, but its graph capability
check still rejected those full identifiers.
Parser errors expose only authored
stage codes such as `trait access/records`; no Google response content is
logged. MIDImap pins an instance to the exact installed package version. To
try 0.1.3 with an older instance, import the 0.1.3 archive and create a new
instance; connect its isolated browser profile and choose the home again.
Older instances and their saved sessions remain separate until disconnected.

The current account test established discovery, read-only state and access
parsing, and one user-initiated on request whose returned power state matched
the request. No integration-initiated live write has been qualified. The
development archive still needs a coordinated first write and physical/state
check before distribution. No real account data belongs in fixtures or the
archive.
