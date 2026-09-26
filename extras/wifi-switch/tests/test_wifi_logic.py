import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import credentials  # noqa: E402
import wifi_controller  # noqa: E402
from wifi_controller import (  # noqa: E402
    JoinRefused,
    NetworkNotFound,
    choose_target,
    classify_join_output,
    parse_airport_ssid,
    parse_preferred_networks,
)


class FakeController:
    """Stands in for a real adapter; never touches the system."""

    def __init__(self, current):
        self.current = current
        self.joins = []

    def get_current_network(self):
        return self.current

    def connect(self, network, password=None):
        if self.current == network:
            # Mirrors the real controllers: never re-join the active network.
            return
        self.joins.append(network)
        self.current = network


def test_switch_from_network1():
    assert choose_target("Home", "Home", "Office") == "Office"


def test_switch_from_network2():
    assert choose_target("Office", "Home", "Office") == "Home"


def test_unknown_network_defaults_to_network1():
    assert choose_target("Coffee Shop", "Home", "Office") == "Home"


def test_undetected_network_defaults_to_network1():
    assert choose_target(None, "Home", "Office") == "Home"


def test_fake_connection():
    controller = FakeController("Home")
    target = choose_target(controller.get_current_network(), "Home", "Office")
    controller.connect(target)

    assert controller.get_current_network() == "Office"
    assert controller.joins == ["Office"]


def test_never_rejoins_the_active_network():
    controller = FakeController("Home")
    controller.connect("Home")

    assert controller.joins == []
    assert controller.get_current_network() == "Home"


# --- macOS output parsing -------------------------------------------------

SP_JSON = json.dumps({
    "SPAirPortDataType": [{
        "spairport_airport_interfaces": [
            {
                "_name": "en0",
                "spairport_current_network_information": {
                    "_name": "az",
                    "spairport_network_channel": "6 (2GHz, 20MHz)",
                },
            },
            {"_name": "awdl0"},
        ]
    }]
})


def test_parse_airport_ssid():
    assert parse_airport_ssid(SP_JSON, "en0") == "az"


def test_parse_airport_ssid_when_not_associated():
    payload = json.dumps({
        "SPAirPortDataType": [{
            "spairport_airport_interfaces": [{"_name": "en0"}]
        }]
    })
    assert parse_airport_ssid(payload, "en0") is None


def test_parse_airport_ssid_with_garbage_input():
    assert parse_airport_ssid("not json", "en0") is None


def test_parse_preferred_networks():
    text = "Preferred networks on en0:\n\taz\n\taz-5G\n\tTP-Link_9BD8\n"
    assert parse_preferred_networks(text) == ["az", "az-5G", "TP-Link_9BD8"]


def test_successful_join_produces_no_error():
    assert classify_join_output("") is None


def test_join_refusal_is_detected_despite_exit_code_zero():
    output = (
        "Failed to join network az.\n"
        "Error: -3900 The operation couldn't be completed. tmpErr"
    )
    assert isinstance(classify_join_output(output), JoinRefused)


def test_missing_network_is_detected():
    output = "Could not find network Office."
    assert isinstance(classify_join_output(output), NetworkNotFound)


# --- join retry behaviour -------------------------------------------------

class _StubbedMac(wifi_controller.MacOSWiFiController):
    """MacOSWiFiController with the two system calls replaced."""

    RETRY_DELAY = 0

    def __init__(self, ssid_sequence, join_outputs):
        super().__init__()
        self._cached_interface = "en0"
        self.ssid_sequence = list(ssid_sequence)
        self.join_outputs = list(join_outputs)
        self.joins = 0

    def get_current_network(self):
        if len(self.ssid_sequence) > 1:
            return self.ssid_sequence.pop(0)
        return self.ssid_sequence[0]

    def _join(self, command):
        self.joins += 1
        return self.join_outputs.pop(0) if self.join_outputs else ""


def _patch_run(monkeypatch, controller):
    def fake_run(command, timeout=20):
        class R:
            returncode = 0
            stdout = controller._join(command)
            stderr = ""
        return R()
    monkeypatch.setattr(wifi_controller, "_run", fake_run)


def test_join_that_reports_failure_but_actually_connected_is_not_retried(monkeypatch):
    # macOS prints "Failed to join" on a join that lands; the old code retried,
    # and the retry re-joined the now-active network, tearing it back down.
    c = _StubbedMac(["Home", "Office"], ["Failed to join network Office.\nError: -3900"])
    _patch_run(monkeypatch, c)

    c.connect("Office")

    assert c.joins == 1


def test_genuine_refusal_still_raises(monkeypatch):
    c = _StubbedMac(["Home"], ["Failed to join network Office.\nError: -3900"] * 3)
    _patch_run(monkeypatch, c)

    with pytest.raises(JoinRefused):
        c.connect("Office")

    assert c.joins == 3


def test_join_is_skipped_when_already_on_the_target(monkeypatch):
    c = _StubbedMac(["Office"], [])
    _patch_run(monkeypatch, c)

    c.connect("Office")

    assert c.joins == 0


# --- credential storage ---------------------------------------------------

def test_password_roundtrip_and_delete():
    ssid = "wifi-switch-unit-test"
    credentials.set_password(ssid, "s3cret")
    try:
        assert credentials.get_password(ssid) == "s3cret"
        assert credentials.has_password(ssid)
    finally:
        credentials.delete_password(ssid)
    assert credentials.get_password(ssid) is None


def test_blank_password_clears_the_entry():
    ssid = "wifi-switch-unit-test-2"
    credentials.set_password(ssid, "s3cret")
    credentials.set_password(ssid, "")
    assert credentials.get_password(ssid) is None
