# Wi-Fi Switch

A tiny desktop utility that switches a laptop between two configured Wi-Fi networks with one button.

The app is deliberately simple:

```text
Current Wi-Fi
     ↓
 [ SWITCH ]
     ↓
Other configured Wi-Fi
```

It performs a real operating-system Wi-Fi connection rather than changing a fake UI state.

## Supported systems

- Windows
- macOS

Linux is intentionally not implemented in this first prototype.

## Requirements

- Python 3.11+ recommended
- Tkinter
- A laptop with Wi-Fi
- The two target Wi-Fi networks should already be saved/known to the operating system, or otherwise be available for connection through the OS.

No runtime third-party package is required.

## Run

From this folder:

```bash
python app.py
```

On some systems you may need:

```bash
python3 app.py
```

## Configure

Open **Settings** and enter:

- Network 1: the Wi-Fi SSID/profile name, plus an optional password
- Network 2: the Wi-Fi SSID/profile name, plus an optional password

The JSON configuration holds only the two names. Passwords are optional and are stored separately:

- **macOS**: in the login keychain, under the service `wifi-switch` (one item per SSID).
- **Elsewhere**: in memory for the current run only. Windows joins a saved profile and takes no
  password at all.

Filling them in ahead of time is worth doing if a network keeps refusing the join: the app then
supplies the password with the join instead of interrupting the switch to ask for it. Leave a
password field blank to keep whatever is already stored; **Forget passwords** removes them.

If a switch is refused and no password is stored, the app asks once and then saves the answer, so
the same switch is not interrupted twice.

One caveat: `networksetup` only accepts a password as a command-line argument, so it is briefly
visible to `ps` on a shared machine while the join runs.

## Switch

If currently connected to Network 1, pressing **SWITCH** connects to Network 2.

If currently connected to Network 2, pressing **SWITCH** connects to Network 1.

If currently connected to neither configured network, pressing **SWITCH** attempts to connect to Network 1.

If the machine is already on the target network, **SWITCH** does nothing instead of dropping and
rejoining the same network.

The app re-reads the live SSID at the moment you press **SWITCH** (never a cached value), then waits
up to 60 seconds for the connected SSID to actually match the target before reporting success.

## Windows

The Windows implementation uses `netsh wlan`.

It detects the active SSID with:

```text
netsh wlan show interfaces
```

and connects to a saved Wi-Fi profile with:

```text
netsh wlan connect name="PROFILE_NAME"
```

The profile must already exist in Windows for the simple prototype to connect without separately creating a Wi-Fi profile.

## macOS

The macOS implementation uses `networksetup` to join, but **not** to read the current SSID.

Two macOS behaviours matter here:

- `networksetup -getairportnetwork en0` answers *"You are not associated with an AirPort network"*
  even while the machine is connected. Trusting it makes the app believe it is offline, so it
  "switches" to Network 1 — the network it is already on. The SSID is therefore read from:

  ```text
  system_profiler -json SPAirPortDataType
  ```

  (`-detailLevel mini` omits the SSID, so the full level is required; a read takes a few seconds,
  which is why all reads run off the UI thread.)

- `networksetup -setairportnetwork` **exits 0 even when the join fails**, printing `Failed to join
  network X` or `Could not find network X` instead. The app classifies that output rather than the
  exit code.

Joining the SSID you are already associated with is what produces `Error: -3900 ... tmpErr`, so the
app never issues a join for the active network. macOS also reports `-3900` on joins that *do* land,
because it answers before the association settles — so between retries the app re-checks the live
SSID and stops as soon as the target is up. Retrying blindly would re-join the network it had just
joined and tear the fresh connection straight back down.

Settings offers the machine's known networks from:

```text
networksetup -listpreferredwirelessnetworks <interface>
```

## Tests

Install pytest:

```bash
python -m pip install -r requirements.txt
```

Run tests:

```bash
python -m pytest -q
```

The unit tests do not change the real Wi-Fi connection. The credential tests write and delete a
throwaway `wifi-switch-unit-test` keychain item and leave your real passwords untouched.

## Limitations

- This is a manual two-network switch, not an autonomous network manager.
- It does not predict network quality.
- It does not manage cellular, 5G, satellite, Ethernet, VPN, QUIC, or edge-compute sessions.
- It has one radio, so a switch is a real outage: the link drops and DHCP has to run again.
- Actual Wi-Fi switching depends on the operating system, adapter, permissions, saved profiles, and network availability.

## Relationship to ECHO

This prototype takes only the simple same-type handoff idea:

```text
Wi-Fi A → Wi-Fi B
```

It intentionally does not implement ECHO's larger watcher/predictor/agent/recovery architecture.
