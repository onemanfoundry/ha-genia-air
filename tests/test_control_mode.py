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
    return m


@pytest.fixture(autouse=True)
def _clean_state(mod, monkeypatch):
    """Each test starts read-only, with an empty STATE, a fake clock, and
    writes/notifications captured instead of hitting the network."""
    with mod.STATE_LOCK:
        mod.STATE.clear()
    mod.CONTROL_ENABLED = False
    mod.CONTROL_EXPIRES_AT = None
    mod.CONTROL_LAST_ACK_AT = None
    mod.CONTROL_SNAPSHOT = None

    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(mod.time, "time", lambda: clock["now"])

    writes: list[tuple[str, str, object]] = []
    monkeypatch.setattr(mod, "mqtt_publish_write",
                         lambda circuit, msg, value: writes.append((circuit, msg, value)))

    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_notify_ha",
                         lambda title, message: notifications.append((title, message)))

    yield {"clock": clock, "writes": writes, "notifications": notifications}


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
