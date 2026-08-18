"""Regression tests for the add-on's derived-value logic.

These exercise the pure functions in `genia_air.py` that compute the values
shown in the UI and used by the optimizer — the exact code paths behind past
bugs (0.0 °C target while cooling, the "UNKNOWN" activity pill, COP/ΔT). They
do NOT touch MQTT, ebusd or HA: the module is imported with GENIA_AIR_TESTING
set so it has no import-time side effects.

Run: `pytest tests/test_logic.py`
"""
from __future__ import annotations

import importlib.util
import os
import pathlib

import pytest

_SRC = (
    pathlib.Path(__file__).parent.parent
    / "genia_air" / "rootfs" / "usr" / "bin" / "genia_air.py"
)


@pytest.fixture(scope="session")
def mod(tmp_path_factory):
    os.environ["GENIA_AIR_TESTING"] = "1"
    os.environ["GENIA_AIR_DATA"] = str(tmp_path_factory.mktemp("data"))
    spec = importlib.util.spec_from_file_location("genia_air_app", _SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _clean_state(mod):
    """Each test starts from an empty STATE."""
    with mod.STATE_LOCK:
        mod.STATE.clear()
    yield


def _set(mod, circuit, msg, fields):
    with mod.STATE_LOCK:
        mod.STATE[(circuit, msg)] = {"fields": fields, "raw": "", "ts": 0.0}


# ── payload decoding ────────────────────────────────────────────────────────

def test_decode_payload_json_nested_value(mod):
    fields, _ = mod._decode_payload('{"tempv": {"value": 21.5}, "n": 3}')
    assert fields == {"tempv": 21.5, "n": 3}


def test_decode_payload_non_json_scalar(mod):
    # A non-JSON payload is wrapped as {"value": ...} (ebusd --mqttjson emits
    # objects; this is the fallback for anything that isn't valid JSON).
    fields, _ = mod._decode_payload("idle")
    assert fields == {"value": "idle"}


# ── ΔT and COP ──────────────────────────────────────────────────────────────

def test_delta_t(mod):
    _set(mod, "hmu", "Status01", {"0": 35.0, "1": 30.0})
    assert mod.compute_delta_t() == 5.0


def test_delta_t_missing_returns_none(mod):
    assert mod.compute_delta_t() is None


def test_cop_idle_is_none(mod):
    _set(mod, "hmu", "CurrentConsumedPower", {"0": 0.0})
    _set(mod, "hmu", "CurrentYieldPower", {"0": 4.0})
    assert mod.compute_cop() is None


def test_cop_running(mod):
    _set(mod, "hmu", "CurrentConsumedPower", {"0": 1.0})
    _set(mod, "hmu", "CurrentYieldPower", {"0": 4.2})
    assert mod.compute_cop() == 4.2


# ── HVAC mode ───────────────────────────────────────────────────────────────

def test_mode_system_off(mod):
    _set(mod, "ctls2", "GlobalSystemOff", {"yesno": "yes"})
    assert mod.compute_hvac_mode() == "off"


def test_mode_heat_only(mod):
    _set(mod, "ctls2", "z1OpMode", {"opmode": "auto"})
    _set(mod, "ctls2", "z1OpModeCooling", {"opmode": "off"})
    assert mod.compute_hvac_mode() == "heat"


def test_mode_cool_only(mod):
    _set(mod, "ctls2", "z1OpMode", {"opmode": "off"})
    _set(mod, "ctls2", "z1OpModeCooling", {"opmode": "auto"})
    assert mod.compute_hvac_mode() == "cool"


def test_mode_auto(mod):
    _set(mod, "ctls2", "z1OpMode", {"opmode": "day"})
    _set(mod, "ctls2", "z1OpModeCooling", {"opmode": "auto"})
    assert mod.compute_hvac_mode() == "auto"


# ── HVAC action — including the fallback that replaced "unknown" ─────────────

def test_action_mapped_state(mod):
    _set(mod, "hmu", "State", {"3": "9"})
    assert mod.compute_hvac_action() == "heating"


def test_action_idle_when_no_power(mod):
    # No State, no compressor activity → idle (not "unknown").
    assert mod.compute_hvac_action() == "idle"


def test_action_fallback_cooling_from_compressor(mod):
    # State code unmapped, but compressor is running and mode is cool.
    _set(mod, "hmu", "State", {"3": "99"})
    _set(mod, "hmu", "CurrentCompressorUtil", {"0": 55.0})
    _set(mod, "ctls2", "z1OpMode", {"opmode": "off"})
    _set(mod, "ctls2", "z1OpModeCooling", {"opmode": "auto"})
    assert mod.compute_hvac_action() == "cooling"


def test_action_auto_uses_delta_t_sign(mod):
    # AUTO + running, negative ΔT (supply colder than return) → cooling.
    _set(mod, "hmu", "CurrentConsumedPower", {"0": 1.2})
    _set(mod, "hmu", "Status01", {"0": 12.0, "1": 18.0})  # ΔT = -6
    _set(mod, "ctls2", "z1OpMode", {"opmode": "auto"})
    _set(mod, "ctls2", "z1OpModeCooling", {"opmode": "auto"})
    assert mod.compute_hvac_action() == "cooling"


# ── Effective setpoint — regression for "Current setpoint: 0.0 °C" ──────────

def test_setpoint_effective_cooling_ignores_zero_heating(mod):
    # Summer: heating desired reports 0.0, cooling setpoint is the real target.
    _set(mod, "ctls2", "z1ActualRoomTempDesired", {"tempv": 0.0})
    _set(mod, "ctls2", "z1ManualTemp", {"tempv": 0.0})
    _set(mod, "ctls2", "z1CoolingTemp", {"tempv": 24.0})
    _set(mod, "ctls2", "z1OpMode", {"opmode": "off"})
    _set(mod, "ctls2", "z1OpModeCooling", {"opmode": "auto"})
    assert mod.compute_setpoint_effective() == 24.0


def test_setpoint_effective_heating(mod):
    _set(mod, "ctls2", "z1ActualRoomTempDesired", {"tempv": 21.0})
    _set(mod, "ctls2", "z1CoolingTemp", {"tempv": 0.0})
    _set(mod, "ctls2", "z1OpMode", {"opmode": "auto"})
    _set(mod, "ctls2", "z1OpModeCooling", {"opmode": "off"})
    assert mod.compute_setpoint_effective() == 21.0


# ── Compatibility check — device identification against KNOWN_GOOD_DEVICES ──

def test_parse_scan_result_single_line(mod):
    text = "08;Vaillant;HMU00;0901;5103;21;07;45;0010002779;0006\n"
    devices = mod._parse_scan_result(text)
    assert devices == [{
        "address": "08", "manufacturer": "Vaillant", "id": "HMU00",
        "sw_version": "0901", "hw_version": "5103",
    }]


def test_parse_scan_result_ignores_comments_and_junk(mod):
    text = "# comment\n\nnot;a;valid;device;line;zz\n15;Vaillant;CTLS2;0509;1304\n"
    devices = mod._parse_scan_result(text)
    assert [d["id"] for d in devices] == ["CTLS2"]


def test_compat_check_matched_exact_reference(mod, monkeypatch):
    monkeypatch.setattr(
        mod, "_ebusd_tcp_command",
        lambda cmd, **kw: "08;Vaillant;HMU00;0901;5103\n15;Vaillant;CTLS2;0509;1304\n",
    )
    result = mod.compat_check()
    assert {d["id"] for d in result["matched"]} == {"HMU00", "CTLS2"}
    assert result["mismatched_firmware"] == []
    assert result["unknown_device"] == []


def test_compat_check_flags_firmware_mismatch(mod, monkeypatch):
    monkeypatch.setattr(
        mod, "_ebusd_tcp_command",
        lambda cmd, **kw: "08;Vaillant;HMU00;0999;5103\n",
    )
    result = mod.compat_check()
    assert result["matched"] == []
    assert [d["id"] for d in result["mismatched_firmware"]] == ["HMU00"]


def test_compat_check_flags_unknown_device(mod, monkeypatch):
    monkeypatch.setattr(
        mod, "_ebusd_tcp_command",
        lambda cmd, **kw: "08;Vaillant;EHP00;0327;7201\n",
    )
    result = mod.compat_check()
    assert result["matched"] == []
    assert result["mismatched_firmware"] == []
    assert [d["id"] for d in result["unknown_device"]] == ["EHP00"]


def test_message_schema_snapshot_has_field_names_not_values(mod):
    _set(mod, "hmu", "Status01", {"0": 35.0, "1": 30.0})
    schema = mod._message_schema_snapshot()
    assert schema == [{"circuit": "hmu", "message": "Status01", "fields": ["0", "1"]}]
    # The whole point is anonymity: no live values anywhere in the output.
    assert "35.0" not in str(schema) and "30.0" not in str(schema)
