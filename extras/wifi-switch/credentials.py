"""Optional Wi-Fi passwords, supplied before a switch instead of mid-switch.

Passwords never go into the JSON configuration. On macOS they live in the
login keychain; elsewhere they are kept in memory for the current run only
(Windows joins saved profiles and does not take a password at all).
"""

import platform
import shutil
import subprocess

SERVICE = "wifi-switch"

_MEMORY_STORE = {}


def _is_macos():
    return platform.system() == "Darwin" and shutil.which("security") is not None


def backend():
    return "keychain" if _is_macos() else "memory"


def _security(args, timeout=15):
    return subprocess.run(
        ["security"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def set_password(ssid, password):
    """Store (or clear, when password is falsy) the password for `ssid`."""
    if not ssid:
        return
    if not password:
        delete_password(ssid)
        return

    if _is_macos():
        # -U updates an existing item; -T lets `security` read it back without
        # a keychain access prompt on every switch.
        _security([
            "add-generic-password",
            "-U",
            "-s", SERVICE,
            "-a", ssid,
            "-w", password,
            "-T", "/usr/bin/security",
        ])
    else:
        _MEMORY_STORE[ssid] = password


def get_password(ssid):
    if not ssid:
        return None

    if _is_macos():
        result = _security([
            "find-generic-password", "-s", SERVICE, "-a", ssid, "-w"
        ])
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\n") or None

    return _MEMORY_STORE.get(ssid)


def has_password(ssid):
    return get_password(ssid) is not None


def delete_password(ssid):
    if not ssid:
        return
    if _is_macos():
        _security(["delete-generic-password", "-s", SERVICE, "-a", ssid])
    else:
        _MEMORY_STORE.pop(ssid, None)
