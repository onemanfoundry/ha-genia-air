"""Regression tests for the read-only/full-control safety gate.

Full control must default OFF, every write path must be gated behind it, a
session must expire on its own, and reverting must actually restore the
pre-session values (not just stop writing) and notify. Same import pattern
as test_logic.py: GENIA_AIR_TESTING avoids MQTT/ebusd/HA side effects.

Run: `pytest tests/test_control_mode.py`
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
    spec = importlib.util.spec_from_file_location("genia_air_app_control", _SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.db_init()  # task_optimize()/collect_snapshot() query the snapshots table
    m._real_mqtt_publish_write = m.mqtt_publish_write  # unpatched, for guard-logic tests
    return m


class _FakeEbusdProcess:
    """Stands in for the real subprocess.Popen handle — poll() returning
    None means "still running", matching subprocess semantics."""
    def poll(self):
        return None


@pytest.fixture(autouse=True)
def _clean_state(mod, monkeypatch):
    """Each test starts read-only, with an empty STATE, a fake clock, a
    HEALTHY link (ebusd running + MQTT connected — individual tests break
    this deliberately to exercise the fail-closed path), and
    writes/notifications captured instead of hitting the network."""
    with mod.STATE_LOCK:
        mod.STATE.clear()
    mod.CONTROL_ENABLED = False
    mod.CONTROL_EXPIRES_AT = None
    mod.CONTROL_LAST_ACK_AT = None
    mod.CONTROL_SNAPSHOT = None
    mod.EBUSD_PROCESS = _FakeEbusdProcess()
    mod.MQTT_CONNECTED.set()

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])

    writes: list[tuple[str, str, object]] = []
    write_ok = {"value": True}
    monkeypatch.setattr(
        mod, "mqtt_publish_write",
        lambda circuit, msg, value: (writes.append((circuit, msg, value)), write_ok["value"])[1]
    )

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_notify_ha",
                         lambda title, message: notifications.append((title, message)))

    yield {"clock": clock, "writes": writes, "notifications": notifications, "write_ok": write_ok}


def _seed_state(mod, circuit, msg, **fields):
    with mod.STATE_LOCK:
        mod.STATE[(circuit, msg)] = {"fields": fields, "raw": "", "ts": mod.time.time()}


# ── default posture ─────────────────────────────────────────────────────────

def test_read_only_by_default(mod):
    assert mod.control_is_active() is False


def test_write_endpoints_reject_when_read_only(mod):
    client = mod.app.test_client()
    assert client.post("/api/write", json={"circuit": "ctls2", "msg": "z1ManualTemp", "value": 21}).status_code == 403
    assert client.post("/api/mode", json={"mode": "heat"}).status_code == 403
    assert client.post("/api/setpoint", json={"target_c": 21}).status_code == 403


def test_enable_requires_explicit_confirm_flag(mod):
    client = mod.app.test_client()
    r = client.post("/api/control/enable", json={})
    assert r.status_code == 400
    assert mod.control_is_active() is False


# ── enabling / snapshotting ─────────────────────────────────────────────────

def test_enable_activates_and_snapshots_current_values(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    _seed_state(mod, "ctls2", "z1OpMode", opmode="auto")

    result = mod.control_enable()

    assert mod.control_is_active() is True
    assert mod.CONTROL_SNAPSHOT["z1ManualTemp"] == {"circuit": "ctls2", "value": 21.0}
    assert mod.CONTROL_SNAPSHOT["z1OpMode"] == {"circuit": "ctls2", "value": "auto"}
    # Every writable field is always present in the snapshot (value None if
    # never read from the bus yet) — not just the ones we happened to seed.
    assert result["snapshot_total"] == len(mod.WRITABLE_MSGS)
    assert result["snapshot_known"] == 2


def test_write_endpoints_accepted_once_control_active(mod, _clean_state):
    mod.control_enable()
    client = mod.app.test_client()
    r = client.post("/api/setpoint", json={"target_c": 22})
    assert r.status_code == 200
    assert _clean_state["writes"][-1] == ("ctls2", "z1ManualTemp", 22.0)


def test_renewing_active_session_does_not_reset_snapshot(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable()
    first_snapshot = mod.CONTROL_SNAPSHOT

    # Value changes mid-session (as if the user wrote a new setpoint)...
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=24.0)
    mod.control_enable()  # renew, e.g. user clicked "ack" style re-enable

    # ...but the ORIGINAL pre-session baseline must survive, not get overwritten.
    assert mod.CONTROL_SNAPSHOT is first_snapshot
    assert mod.CONTROL_SNAPSHOT["z1ManualTemp"]["value"] == 21.0


# ── expiry / stale-ack revert ───────────────────────────────────────────────

def test_session_expires_and_restores_snapshot(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable(session_minutes=10)

    _clean_state["clock"]["now"] += 10 * 60 + 1
    mod.task_control_watchdog()

    assert mod.control_is_active() is False
    assert ("ctls2", "z1ManualTemp", 21.0) in _clean_state["writes"]
    assert len(_clean_state["notifications"]) == 1


def test_stale_ack_reverts_before_hard_expiry(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable(session_minutes=240)  # long session...

    # ...but nobody re-confirms within the grace window.
    _clean_state["clock"]["now"] += mod.CONF["control_ack_grace_min"] * 60 + 1
    mod.task_control_watchdog()

    assert mod.control_is_active() is False
    assert ("ctls2", "z1ManualTemp", 21.0) in _clean_state["writes"]


def test_ack_resets_the_grace_clock(mod, _clean_state):
    mod.control_enable(session_minutes=240)
    grace = mod.CONF["control_ack_grace_min"] * 60

    _clean_state["clock"]["now"] += grace - 5
    mod.control_ack()
    _clean_state["clock"]["now"] += grace - 5
    mod.task_control_watchdog()

    # Still active — the ack should have pushed the deadline forward.
    assert mod.control_is_active() is True


def test_manual_disable_reverts_immediately(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=19.5)
    mod.control_enable()
    client = mod.app.test_client()

    r = client.post("/api/control/disable")

    assert r.status_code == 200
    assert mod.control_is_active() is False
    assert ("ctls2", "z1ManualTemp", 19.5) in _clean_state["writes"]


def test_optimizer_does_not_write_when_read_only(mod, _clean_state):
    mod.OPTIMIZER_ENABLED = True
    _seed_state(mod, "ctls2", "OutsideTempAvg", tempv=5.0)
    mod.task_optimize()
    assert _clean_state["writes"] == []


def test_delta_t_alert_still_runs_when_read_only(mod, _clean_state):
    """Regression: the ΔT anomaly check is pure alerting (no actuation) and
    must keep working in the default read-only state, not get silently
    disabled by the same gate that blocks writes."""
    mod.OPTIMIZER_ENABLED = True
    mod.HEALTH["ok"] = True
    mod.HEALTH["reasons"] = []
    _seed_state(mod, "hmu", "Status01", **{"0": 45.0, "1": 30.0})  # ΔT = 15, way off target
    _seed_state(mod, "hmu", "State", **{"3": "9"})  # → hvac_action "heating"

    mod.task_optimize()

    assert mod.control_is_active() is False
    assert _clean_state["writes"] == []              # still no writes
    assert mod.HEALTH["ok"] is False                  # but the alert still fired
    assert any("ΔT" in r for r in mod.HEALTH["reasons"])


def test_revert_skips_fields_with_no_known_baseline(mod, _clean_state):
    """A field never read from the bus before the session started has no
    real value to restore — control_restore must not publish a bogus one."""
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    # z1DayTemp deliberately left unseeded — unknown at snapshot time.
    mod.control_enable()
    assert mod.CONTROL_SNAPSHOT["z1DayTemp"]["value"] is None

    mod.control_revert("test")

    written_msgs = {msg for _circuit, msg, _value in _clean_state["writes"]}
    assert "z1ManualTemp" in written_msgs
    assert "z1DayTemp" not in written_msgs


def test_snapshot_persisted_and_cleared_on_disk(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable()
    assert mod.CONTROL_SNAPSHOT_PATH.exists()

    mod.control_revert("test")
    assert not mod.CONTROL_SNAPSHOT_PATH.exists()


def test_boot_recovery_restores_stale_session_and_boots_read_only(mod, _clean_state):
    mod.CONTROL_SNAPSHOT_PATH.write_text(
        mod.json.dumps({"z1ManualTemp": {"circuit": "ctls2", "value": 20.0}})
    )

    mod.control_recover_on_boot()

    assert ("ctls2", "z1ManualTemp", 20.0) in _clean_state["writes"]
    assert not mod.CONTROL_SNAPSHOT_PATH.exists()
    assert mod.control_is_active() is False
    assert len(_clean_state["notifications"]) == 1


def test_watchdog_does_not_revert_a_session_renewed_at_the_boundary(mod, _clean_state):
    """Regression for the decide/act race: a renewal that lands before the
    watchdog's check must never get undone by a decision made before it."""
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable(session_minutes=10)

    _clean_state["clock"]["now"] += 10 * 60 - 1  # 1s before expiry
    mod.control_enable(session_minutes=10)        # renewed just in time
    _clean_state["clock"]["now"] += 2             # past the ORIGINAL expiry only

    mod.task_control_watchdog()

    assert mod.control_is_active() is True
    assert _clean_state["notifications"] == []


# ── fail-closed: never fail-open on a broken link ──────────────────────────

def test_watchdog_reverts_when_ebusd_dies_during_a_session(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable(session_minutes=60)
    mod.EBUSD_PROCESS = None  # matches "process not running"

    mod.task_control_watchdog()

    assert mod.control_is_active() is False
    assert len(_clean_state["notifications"]) == 1
    assert "ebusd" in _clean_state["notifications"][0][1]


def test_watchdog_reverts_when_mqtt_disconnects_during_a_session(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable(session_minutes=60)
    mod.MQTT_CONNECTED.clear()

    mod.task_control_watchdog()

    assert mod.control_is_active() is False
    assert "MQTT" in _clean_state["notifications"][0][1]


def test_healthy_session_is_not_touched_by_the_fail_closed_check(mod, _clean_state):
    mod.control_enable(session_minutes=60)
    mod.task_control_watchdog()
    assert mod.control_is_active() is True
    assert _clean_state["notifications"] == []


# ── mqtt_publish_write: the real guard logic (unpatched) ───────────────────

def test_real_publish_rejects_nan(mod):
    assert mod._real_mqtt_publish_write("ctls2", "z1ManualTemp", float("nan")) is False


def test_real_publish_rejects_infinity(mod):
    assert mod._real_mqtt_publish_write("ctls2", "z1ManualTemp", float("inf")) is False


def test_real_publish_rejects_when_mqtt_not_connected(mod):
    mod.MQTT_CONNECTED.clear()
    try:
        assert mod._real_mqtt_publish_write("ctls2", "z1ManualTemp", 21.0) is False
    finally:
        mod.MQTT_CONNECTED.set()  # restore the healthy default other tests rely on


def test_real_publish_accepts_a_normal_value_when_connected(mod):
    mod.MQTT_CLIENT = None  # no real client in tests — publish() would explode
    try:
        mod.MQTT_CLIENT = type("FakeClient", (), {"publish": lambda self, *a, **k: None})()
        assert mod._real_mqtt_publish_write("ctls2", "z1ManualTemp", 21.0) is True
    finally:
        mod.MQTT_CLIENT = None


# ── API endpoints surface a write failure instead of pretending success ────

def test_api_write_returns_503_when_publish_fails(mod, _clean_state):
    mod.control_enable()
    _clean_state["write_ok"]["value"] = False
    client = mod.app.test_client()

    r = client.post("/api/setpoint", json={"target_c": 22})

    assert r.status_code == 503


# ── audit log carries old/new value and a source label ─────────────────────

def test_manual_write_logs_old_and_new_value(mod, _clean_state, monkeypatch):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=19.0)
    mod.control_enable()
    logged = []
    monkeypatch.setattr(mod, "db_log_decision",
                         lambda kind, reason, detail: logged.append((kind, reason, detail)))
    client = mod.app.test_client()

    client.post("/api/setpoint", json={"target_c": 22})

    kind, reason, detail = logged[-1]
    assert kind == "user_setpoint"
    assert detail["old_value"] == 19.0
    assert detail["new_value"] == 22.0
    assert detail["source"] == "manual"


# ── stale telemetry: a frozen sensor is still "present" but must not be trusted ──

def test_watchdog_reverts_on_stale_telemetry(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable(session_minutes=240)  # long session, won't expire on its own

    _clean_state["clock"]["now"] += mod.CONF["stale_data_min"] * 60 + 1  # no fresh data since

    mod.task_control_watchdog()

    assert mod.control_is_active() is False
    assert "congelada" in _clean_state["notifications"][0][1]


def test_watchdog_tolerates_data_fresher_than_the_stale_threshold(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    mod.control_enable(session_minutes=240)

    _clean_state["clock"]["now"] += mod.CONF["stale_data_min"] * 60 - 5
    mod.control_ack()  # keep the (unrelated) ack-grace timer from confounding this test
    _seed_state(mod, "ctls2", "OutsideTempAvg", tempv=10.0)  # fresh update resets the staleness clock
    _clean_state["clock"]["now"] += mod.CONF["stale_data_min"] * 60 - 5
    mod.control_ack()

    mod.task_control_watchdog()

    assert mod.control_is_active() is True


def test_health_check_flags_stale_telemetry(mod, _clean_state):
    _seed_state(mod, "ctls2", "z1ManualTemp", tempv=21.0)
    _clean_state["clock"]["now"] += mod.CONF["stale_data_min"] * 60 + 1

    mod.task_health_check()

    assert mod.HEALTH["ok"] is False
    assert any("stale" in r.lower() for r in mod.HEALTH["reasons"])


# ── simulate mode: exercise the real write path with no real hardware ──────

def test_simulated_mqtt_client_writes_temp_field_into_state(mod):
    client = mod._SimulatedMqttClient()
    with mod.STATE_LOCK:
        mod.STATE.clear()

    client.publish("ebusd/ctls2/z1ManualTemp/set", "23.5")

    assert mod.STATE[("ctls2", "z1ManualTemp")]["fields"]["tempv"] == 23.5


def test_simulated_mqtt_client_writes_mode_field_into_state(mod):
    client = mod._SimulatedMqttClient()
    with mod.STATE_LOCK:
        mod.STATE.clear()

    client.publish("ebusd/ctls2/z1OpMode/set", "auto")

    assert mod.STATE[("ctls2", "z1OpMode")]["fields"]["opmode"] == "auto"


def test_simulated_mqtt_client_ignores_read_requests(mod):
    client = mod._SimulatedMqttClient()
    with mod.STATE_LOCK:
        mod.STATE.clear()

    client.publish("ebusd/ctls2/z1ManualTemp/get", "?")

    assert ("ctls2", "z1ManualTemp") not in mod.STATE


def test_simulate_start_seeds_plausible_state_and_marks_healthy(mod, _clean_state):
    mod.simulate_start()

    assert isinstance(mod.EBUSD_PROCESS, mod._SimulatedProcess)
    assert isinstance(mod.MQTT_CLIENT, mod._SimulatedMqttClient)
    assert mod.MQTT_CONNECTED.is_set()
    assert mod._field("ctls2", "OutsideTempAvg", "tempv") is not None
    mod.task_health_check()
    assert mod.HEALTH["ok"] is True


def test_simulate_mode_full_control_round_trip(mod, _clean_state, monkeypatch):
    """The RV-02 round-trip — enable control, write a setpoint, confirm the
    simulated bus reflects it, let the session expire, confirm the original
    value is restored for real — run through the REAL mqtt_publish_write
    (not the per-test mock), against the simulator instead of real hardware.
    This is the closest thing to a physical round-trip test until real
    hardware is available again."""
    monkeypatch.setattr(mod, "mqtt_publish_write", mod._real_mqtt_publish_write)
    mod.simulate_start()
    client = mod.app.test_client()
    baseline = mod._field("ctls2", "z1ManualTemp", "tempv")
    assert baseline is not None

    mod.control_enable(session_minutes=10)
    r = client.post("/api/setpoint", json={"target_c": 24.0})
    assert r.status_code == 200
    assert mod._field("ctls2", "z1ManualTemp", "tempv") == 24.0

    _clean_state["clock"]["now"] += 10 * 60 + 1
    mod.task_control_watchdog()

    assert mod.control_is_active() is False
    assert mod._field("ctls2", "z1ManualTemp", "tempv") == baseline
