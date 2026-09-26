import json
import platform
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod


class WiFiError(RuntimeError):
    pass


class NetworkNotFound(WiFiError):
    """The target SSID is not in range / not visible to the adapter."""


class JoinRefused(WiFiError):
    """The adapter saw the network but the association was refused.

    On macOS this is the '-3900 tmpErr' family: a transient refusal, not a
    wrong password. Retrying, or supplying the password explicitly, usually
    clears it.
    """


# macOS prints join errors on stdout and still exits 0, so the text is the
# only reliable signal.
_NOT_FOUND_MARKERS = (
    "could not find network",
    "network not found",
)
_REFUSED_MARKERS = (
    "failed to join",
    "could not connect",
    "error: -",
)


def _run(command, timeout=20):
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise WiFiError(f"Required system command was not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise WiFiError(f"System command timed out: {' '.join(command)}") from exc


def choose_target(current, network1, network2):
    """Pick the network to switch to, given the SSID we are on right now."""
    if current == network1:
        return network2
    if current == network2:
        return network1
    return network1


def classify_join_output(output):
    """Return an exception for a failed join, or None when the join was accepted.

    `networksetup -setairportnetwork` exits 0 even when it fails, so the
    command output has to be classified instead of the return code.
    """
    text = (output or "").strip()
    low = text.lower()
    if not low:
        return None
    if any(marker in low for marker in _NOT_FOUND_MARKERS):
        return NetworkNotFound(text)
    if any(marker in low for marker in _REFUSED_MARKERS):
        return JoinRefused(text)
    return None


def parse_airport_ssid(json_text, interface):
    """Pull the associated SSID for `interface` out of system_profiler JSON."""
    try:
        data = json.loads(json_text)
    except (TypeError, ValueError):
        return None

    for entry in data.get("SPAirPortDataType", []):
        for itf in entry.get("spairport_airport_interfaces", []):
            if itf.get("_name") != interface:
                continue
            current = itf.get("spairport_current_network_information") or {}
            ssid = current.get("_name")
            if isinstance(ssid, str) and ssid.strip():
                return ssid.strip()
            return None
    return None


def parse_preferred_networks(text):
    """Parse `networksetup -listpreferredwirelessnetworks` output."""
    networks = []
    for line in (text or "").splitlines():
        if line.strip().lower().startswith("preferred networks"):
            continue
        ssid = line.strip()
        if ssid and ssid not in networks:
            networks.append(ssid)
    return networks


class BaseWiFiController(ABC):
    @abstractmethod
    def get_current_network(self):
        raise NotImplementedError

    @abstractmethod
    def get_saved_networks(self):
        raise NotImplementedError

    @abstractmethod
    def connect(self, network, password=None):
        raise NotImplementedError

    def is_connected(self, network):
        return self.get_current_network() == network


class WindowsWiFiController(BaseWiFiController):
    def _check_netsh(self):
        if not shutil.which("netsh"):
            raise WiFiError("netsh is not available on this Windows installation.")

    def get_current_network(self):
        self._check_netsh()
        result = _run(["netsh", "wlan", "show", "interfaces"])
        if result.returncode != 0:
            raise WiFiError(result.stderr.strip() or result.stdout.strip())

        # Handles localized Windows output reasonably by looking for the
        # SSID field while excluding BSSID.
        for line in result.stdout.splitlines():
            if re.match(r"^\s*SSID\s*:", line, re.IGNORECASE):
                return line.split(":", 1)[1].strip() or None
        return None

    def get_saved_networks(self):
        self._check_netsh()
        result = _run(["netsh", "wlan", "show", "profiles"])
        if result.returncode != 0:
            raise WiFiError(result.stderr.strip() or result.stdout.strip())

        profiles = []
        for line in result.stdout.splitlines():
            match = re.search(r":\s*(.+?)\s*$", line)
            if match and ("All User Profile" in line or "User Profile" in line):
                profiles.append(match.group(1).strip())
        return profiles

    def connect(self, network, password=None):
        self._check_netsh()
        if self.is_connected(network):
            return
        result = _run(["netsh", "wlan", "connect", f"name={network}"], timeout=20)
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip()
            raise WiFiError(msg or f"Windows could not connect to '{network}'.")


class MacOSWiFiController(BaseWiFiController):
    JOIN_ATTEMPTS = 3
    RETRY_DELAY = 2.0

    def __init__(self):
        self._cached_interface = None

    def _interface(self):
        if self._cached_interface:
            return self._cached_interface

        result = _run(["networksetup", "-listallhardwareports"])
        if result.returncode != 0:
            raise WiFiError(result.stderr.strip() or result.stdout.strip())

        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "Hardware Port: Wi-Fi" and i + 1 < len(lines):
                m = re.search(r"Device:\s*(\S+)", lines[i + 1])
                if m:
                    self._cached_interface = m.group(1)
                    return self._cached_interface

        # Common fallback; still verify networksetup exists at all.
        if shutil.which("networksetup"):
            self._cached_interface = "en0"
            return self._cached_interface

        raise WiFiError("Could not find the macOS Wi-Fi interface.")

    def get_current_network(self):
        interface = self._interface()

        # system_profiler is the only source that stays accurate on recent
        # macOS. `networksetup -getairportnetwork` reports "You are not
        # associated with an AirPort network" even while connected, which
        # would make the app think it is offline and re-join the network it
        # is already on. It is kept only as a fallback.
        ssid = self._ssid_from_system_profiler(interface)
        if ssid:
            return ssid
        return self._ssid_from_networksetup(interface)

    def _ssid_from_system_profiler(self, interface):
        # A full scan takes a few seconds; -detailLevel mini omits the SSID.
        result = _run(["system_profiler", "-json", "SPAirPortDataType"], timeout=40)
        if result.returncode != 0:
            return None
        return parse_airport_ssid(result.stdout, interface)

    def _ssid_from_networksetup(self, interface):
        result = _run(["networksetup", "-getairportnetwork", interface])
        text = result.stdout.strip()
        if result.returncode != 0 and not text:
            raise WiFiError(result.stderr.strip() or "Wi-Fi status is unavailable.")

        if text.lower().startswith("you are not associated"):
            return None
        match = re.search(r":\s*(.+)$", text)
        if not match:
            return None
        return match.group(1).strip() or None

    def get_saved_networks(self):
        interface = self._interface()
        result = _run(
            ["networksetup", "-listpreferredwirelessnetworks", interface]
        )
        if result.returncode != 0:
            return []
        return parse_preferred_networks(result.stdout)

    def set_power(self, on):
        interface = self._interface()
        _run(["networksetup", "-setairportpower", interface, "on" if on else "off"])

    def connect(self, network, password=None):
        interface = self._interface()

        # Re-joining the SSID you are already associated with is what produces
        # the "-3900 tmpErr" refusal, and it drops the connection for nothing.
        if self.get_current_network() == network:
            return

        command = ["networksetup", "-setairportnetwork", interface, network]
        if password:
            command.append(password)

        last_error = None
        for attempt in range(1, self.JOIN_ATTEMPTS + 1):
            result = _run(command, timeout=30)
            output = f"{result.stdout}\n{result.stderr}".strip()
            error = classify_join_output(output)

            if error is None and result.returncode != 0:
                error = WiFiError(
                    output or f"macOS could not connect to '{network}'."
                )
            if error is None:
                return

            last_error = error
            if isinstance(error, NetworkNotFound):
                raise NetworkNotFound(
                    f"'{network}' is not in range right now."
                )

            # macOS reports "Failed to join" / -3900 on joins that do land,
            # because it answers before the association settles. Retrying
            # blindly would re-join a network we are now on, which tears the
            # fresh connection back down — so always confirm first.
            time.sleep(self.RETRY_DELAY)
            if self.get_current_network() == network:
                return

        raise JoinRefused(
            f"macOS refused the join to '{network}' "
            f"({self.JOIN_ATTEMPTS} attempts): {last_error}"
        )


class UnsupportedWiFiController(BaseWiFiController):
    def _error(self):
        raise WiFiError(
            f"Unsupported operating system: {platform.system()}. "
            "This prototype supports Windows and macOS."
        )

    def get_current_network(self):
        self._error()

    def get_saved_networks(self):
        self._error()

    def connect(self, network, password=None):
        self._error()


def create_wifi_controller():
    system = platform.system()
    if system == "Windows":
        return WindowsWiFiController()
    if system == "Darwin":
        return MacOSWiFiController()
    return UnsupportedWiFiController()
