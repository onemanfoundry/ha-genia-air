#!/usr/bin/env python3
"""Vaillant Genia Air — standalone HA addon.

One file. Loop pattern modeled on Energy Optimizer:
  * MQTT client subscribed to ebusd/+/+ (paho)
  * STATE dict (circuit, msg) -> last decoded payload + timestamp
  * SQLite at /data/history.db for snapshots, decisions, errors
  * APScheduler: initial sync, snapshot, optimizer cycle, health
  * Flask app with /api/* JSON + / serving the embedded PANEL HTML
  * MQTT Discovery — six minimal entities back into HA
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

import paho.mqtt.client as mqtt
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, jsonify, request

# ───────────────────────────────────────────────────────────────────────────
# Config & logging
# ───────────────────────────────────────────────────────────────────────────

VERSION = "0.6.0"


def _load_options() -> dict:
    """Read /data/options.json — written by the supervisor from the user config."""
    try:
        with open("/data/options.json") as f:
            opts = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logging.warning("Cannot read /data/options.json: %s", exc)
        return {}
    # If the configured ebus_device is a TCP endpoint and unreachable, try to
    # find a live adapter on the same /24. Helps when an old IP is persisted in
    # the supervisor's options.json after the user moved the adapter.
    dev = str(opts.get("ebus_device", ""))
    m = re.match(r"^(ens|enh|tcp):([^:]+):(\d+)$", dev)
    if m:
        proto, host, port = m.group(1), m.group(2), int(m.group(3))
        if not _tcp_probe(host, port):
            alt = _scan_lan_for_ebus(host, port)
            if alt and alt != host:
                opts["ebus_device"] = f"{proto}:{alt}:{port}"
                print(f"[boot] ebus_device {host}:{port} unreachable, "
                      f"falling back to {alt}:{port}", file=__import__('sys').stderr)
    return opts


def _tcp_probe(host: str, port: int, timeout: float = 1.5) -> bool:
    import socket as _s
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.close()
        return True
    except Exception:
        return False


def _scan_lan_for_ebus(seed_host: str, port: int) -> str | None:
    """Best-effort scan: try the user's /24 (and /24 neighbouring), looking for
    a host that accepts TCP on `port`. Returns the first match (excluding the
    seed and its standard gateway), or None."""
    parts = seed_host.split(".")
    if len(parts) != 4:
        return None
    base = ".".join(parts[:3])
    candidates = [f"{base}.{i}" for i in range(1, 255) if str(i) != parts[3]]
    # Don't scan the whole /24 sequentially — too slow. Use threads.
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=32) as ex:
        futures = {ex.submit(_tcp_probe, host, port, 0.6): host for host in candidates}
        for fut in _cf.as_completed(futures):
            if fut.result():
                return futures[fut]
    return None


def _query_supervisor_mqtt(retries: int = 12, backoff: float = 2.0) -> dict:
    """Ask the supervisor for the MQTT broker config.

    Run at boot, so the supervisor may not be ready yet — retry with backoff.
    Logging isn't configured yet at this point; print to stderr so it survives.
    """
    import sys as _sys
    import time as _t

    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""
    if not token:
        print("[boot] No SUPERVISOR_TOKEN/HASSIO_TOKEN — running outside HA?", file=_sys.stderr)
        return {}
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json().get("data", {})
                print(
                    f"[boot] Got MQTT creds from supervisor on attempt {attempt} "
                    f"(host={data.get('host')}, user={data.get('username')})",
                    file=_sys.stderr,
                )
                return {
                    "host": data.get("host", "core-mosquitto"),
                    "port": int(data.get("port", 1883)),
                    "username": data.get("username", ""),
                    "password": data.get("password", ""),
                }
            print(
                f"[boot] Supervisor /services/mqtt attempt {attempt} → HTTP {r.status_code}",
                file=_sys.stderr,
            )
        except Exception as exc:
            print(
                f"[boot] Supervisor /services/mqtt attempt {attempt} failed: {exc}",
                file=_sys.stderr,
            )
        _t.sleep(backoff)
    print("[boot] Giving up on supervisor MQTT introspection", file=_sys.stderr)
    return {}


# GENIA_AIR_TESTING lets the regression suite import this module without the
# import-time side effects (supervisor/network calls). Production never sets it.
if os.environ.get("GENIA_AIR_TESTING"):
    _opts, _mqtt = {}, {}
else:
    _opts = _load_options()
    _mqtt = _query_supervisor_mqtt()

CONF = {
    "ebus_device":           _opts.get("ebus_device", "ens:192.168.1.100:9999"),
    "ebusd_log_level":       str(_opts.get("ebusd_log_level", "notice")),
    "topic_prefix":          _opts.get("topic_prefix", "ebusd"),
    "zone_count":            int(_opts.get("zone_count", 1)),
    "optimize_flow_temp":    bool(_opts.get("optimize_flow_temp", True)),
    "target_delta_t":        float(_opts.get("target_delta_t", 5.0)),
    "min_flow_temp_safe":    float(_opts.get("min_flow_temp_safe", 14.0)),
    "max_flow_temp_safe":    float(_opts.get("max_flow_temp_safe", 35.0)),
    "summer_temp_limit":     float(_opts.get("summer_temp_limit", 19.0)),
    "optimize_cycle_min":    int(_opts.get("optimize_cycle_minutes", 5)),
    "control_session_min":   int(_opts.get("control_session_minutes", 60)),
    "control_ack_grace_min": int(_opts.get("control_ack_grace_minutes", 15)),
    "control_notify_target": str(_opts.get("control_notify_target", "") or ""),
    "mqtt_host":             _mqtt.get("host", "core-mosquitto"),
    "mqtt_port":             _mqtt.get("port", 1883),
    "mqtt_user":             _mqtt.get("username", ""),
    "mqtt_pass":             _mqtt.get("password", ""),
    "log_level":             str(_opts.get("log_level", "info")).upper(),
}

logging.basicConfig(
    level=getattr(logging, CONF["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("genia_air")
log.info("Genia Air addon v%s starting", VERSION)
log.info("Config: %s", {k: v for k, v in CONF.items() if k != "mqtt_pass"})

DATA = Path(os.environ.get("GENIA_AIR_DATA", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "history.db"
DECISIONS_PATH = DATA / "decisions.jsonl"

# ───────────────────────────────────────────────────────────────────────────
# STATE — live snapshot of ebusd readings
# ───────────────────────────────────────────────────────────────────────────

STATE_LOCK = threading.Lock()
STATE: dict[tuple[str, str], dict] = {}   # (circuit, msg) → {fields, raw, ts}
LAST_DECISIONS: "OrderedDict[float, dict]" = OrderedDict()
OPTIMIZER_ENABLED = CONF["optimize_flow_temp"]
HEALTH = {"ok": True, "reasons": [], "since": time.time()}

# ───────────────────────────────────────────────────────────────────────────
# Control mode — read-only by default. Every actual boiler write (manual
# setpoint/mode changes AND the autonomous optimizer) is gated behind an
# explicit, time-boxed "full control" session the user has to opt into and
# keep periodically re-confirming. See genia_air/README.md "Safety model".
# ───────────────────────────────────────────────────────────────────────────

CONTROL_LOCK = threading.Lock()
CONTROL_ENABLED = False          # False = read-only (default)
CONTROL_EXPIRES_AT: float | None = None   # session hard expiry (epoch seconds)
CONTROL_LAST_ACK_AT: float | None = None  # last "still looks right" confirmation
CONTROL_SNAPSHOT: dict | None = None      # writable values as they were before this session

# (circuit, msg) entries the addon cares about. Drives the initial sync and
# the "Diagnostic" tab. Case must match what ebusd publishes (case rule:
# lowercase first char only if second is a digit, e.g. Z1ManualTemp→z1ManualTemp,
# Hc1MaxFlowTempDesired stays).
SUBSCRIBED_MSGS: list[tuple[str, str]] = [
    # HMU — telemetry
    ("hmu", "CurrentConsumedPower"),
    ("hmu", "CurrentYieldPower"),
    ("hmu", "CurrentCompressorUtil"),
    ("hmu", "WaterThroughput"),
    ("hmu", "Status01"),
    ("hmu", "State"),
    ("hmu", "Hours"),
    ("hmu", "HoursHc"),
    ("hmu", "HoursCool"),
    ("hmu", "errorhistory"),
    # CTLS2 — zone 1 (multi-zone is post-v0.1)
    ("ctls2", "z1RoomTemp"),
    ("ctls2", "z1ActualRoomTempDesired"),
    ("ctls2", "z1ManualTemp"),
    ("ctls2", "z1DayTemp"),
    ("ctls2", "z1NightTemp"),
    ("ctls2", "z1HolidayTemp"),
    ("ctls2", "z1CoolingTemp"),
    ("ctls2", "z1QuickVetoTemp"),
    ("ctls2", "z1OpMode"),
    ("ctls2", "z1OpModeCooling"),
    ("ctls2", "z1SfMode"),
    ("ctls2", "OutsideTempAvg"),
    ("ctls2", "MaintenanceDue"),
    ("ctls2", "GlobalSystemOff"),
    ("ctls2", "YieldTotal"),
    ("ctls2", "Hc1MaxFlowTempDesired"),
    ("ctls2", "Hc1MinFlowTempDesired"),
    ("ctls2", "Hc1SummerTempLimit"),
    ("ctls2", "ContinuosHeating"),
]

# Every (circuit, msg) a full-control session can write to, across the manual
# API (/api/write, /api/mode, /api/setpoint) and the optimizer. This is what
# gets snapshotted before the first write of a session, and restored when the
# session ends (expiry or unacknowledged) — see control_snapshot()/control_restore().
WRITABLE_MSGS: list[tuple[str, str]] = [
    ("ctls2", "z1ManualTemp"),
    ("ctls2", "z1DayTemp"),
    ("ctls2", "z1NightTemp"),
    ("ctls2", "z1HolidayTemp"),
    ("ctls2", "z1CoolingTemp"),
    ("ctls2", "z1OpMode"),
    ("ctls2", "z1OpModeCooling"),
    ("ctls2", "Hc1MaxFlowTempDesired"),
    ("ctls2", "Hc1MinFlowTempDesired"),
    ("ctls2", "Hc1SummerTempLimit"),
    ("ctls2", "ContinuosHeating"),
]

# ───────────────────────────────────────────────────────────────────────────
# Persistence — SQLite for snapshots and decisions
# ───────────────────────────────────────────────────────────────────────────

DB_LOCK = threading.Lock()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def db_init() -> None:
    with DB_LOCK, db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                ts          INTEGER,
                series      TEXT,
                value       REAL,
                PRIMARY KEY (ts, series)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS ix_snapshots_series ON snapshots(series, ts);

            CREATE TABLE IF NOT EXISTS decisions (
                ts          INTEGER PRIMARY KEY,
                kind        TEXT,
                reason      TEXT,
                detail      TEXT
            );

            CREATE TABLE IF NOT EXISTS errors (
                ts          INTEGER PRIMARY KEY,
                code        TEXT,
                detail      TEXT
            );
            """
        )


def db_init_safe() -> None:
    """SQLite up to 3.38 doesn't accept the composite PK syntax above. Fallback
    to a simpler schema if creation failed."""
    try:
        db_init()
    except sqlite3.OperationalError as exc:
        log.warning("db_init: composite PK failed (%s), using fallback", exc)
        with DB_LOCK, db_connect() as conn:
            conn.executescript(
                """
                DROP TABLE IF EXISTS snapshots;
                CREATE TABLE snapshots (
                    ts      INTEGER,
                    series  TEXT,
                    value   REAL
                );
                CREATE INDEX IF NOT EXISTS ix_snapshots_series_ts
                    ON snapshots(series, ts);
                CREATE TABLE IF NOT EXISTS decisions (
                    ts INTEGER PRIMARY KEY, kind TEXT, reason TEXT, detail TEXT
                );
                CREATE TABLE IF NOT EXISTS errors (
                    ts INTEGER PRIMARY KEY, code TEXT, detail TEXT
                );
                """
            )


def db_insert_snapshot(ts: int, series: str, value: float) -> None:
    try:
        with DB_LOCK, db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots(ts, series, value) VALUES (?, ?, ?)",
                (ts, series, value),
            )
    except Exception as exc:
        log.debug("snapshot insert failed for %s: %s", series, exc)


def db_query_series(series: str, hours: int) -> list[tuple[int, float]]:
    since = int(time.time()) - hours * 3600
    with DB_LOCK, db_connect() as conn:
        rows = conn.execute(
            "SELECT ts, value FROM snapshots WHERE series=? AND ts>=? ORDER BY ts",
            (series, since),
        ).fetchall()
    return rows


def db_log_decision(kind: str, reason: str, detail: dict) -> None:
    ts = int(time.time())
    try:
        with DB_LOCK, db_connect() as conn:
            conn.execute(
                "INSERT INTO decisions(ts, kind, reason, detail) VALUES (?, ?, ?, ?)",
                (ts, kind, reason, json.dumps(detail)),
            )
    except Exception:
        pass
    record = {"ts": ts, "kind": kind, "reason": reason, "detail": detail}
    LAST_DECISIONS[ts] = record
    while len(LAST_DECISIONS) > 200:
        LAST_DECISIONS.popitem(last=False)
    try:
        with DECISIONS_PATH.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


# ───────────────────────────────────────────────────────────────────────────
# ebusd subprocess — bundled daemon, supervised
# ───────────────────────────────────────────────────────────────────────────

EBUSD_BIN = "/usr/bin/ebusd"
EBUSD_CONFIG_PATH = "/usr/share/ebusd/vaillant"
EBUSD_PROCESS: subprocess.Popen | None = None
EBUSD_LAST_RESTART = 0.0
EBUSD_RESTART_COUNT = 0


def _ebusd_argv() -> list[str]:
    """Build the ebusd command line."""
    return [
        EBUSD_BIN,
        "--foreground",
        "--device", CONF["ebus_device"],
        "--configpath", EBUSD_CONFIG_PATH,
        "--scanconfig",
        "--accesslevel", "*",
        "--mqtthost", CONF["mqtt_host"],
        "--mqttport", str(CONF["mqtt_port"]),
        "--mqttuser", CONF["mqtt_user"],
        "--mqttpass", CONF["mqtt_pass"],
        "--mqtttopic", CONF["topic_prefix"],
        "--mqttjson",
        "--mqttretain",
        "--log", f"all:{CONF['ebusd_log_level']}",
    ]


def _ebusd_pump_logs() -> None:
    """Background reader: forward ebusd stdout/stderr lines into our logger."""
    proc = EBUSD_PROCESS
    if not proc or not proc.stdout:
        return
    for line in iter(proc.stdout.readline, b""):
        try:
            text = line.decode("utf-8", errors="replace").rstrip()
        except Exception:
            continue
        if not text:
            continue
        # Tag every ebusd line so logs are easy to grep.
        log.info("[ebusd] %s", text)


def ebusd_start() -> None:
    """Spawn ebusd as a child process. Idempotent."""
    global EBUSD_PROCESS
    if EBUSD_PROCESS and EBUSD_PROCESS.poll() is None:
        return
    argv = _ebusd_argv()
    # Don't leak password into the log line, but keep enough to debug.
    safe = [a if "pass" not in argv[i - 1].lower() else "***" for i, a in enumerate(argv)]
    log.info("Spawning ebusd: %s", " ".join(safe))
    try:
        EBUSD_PROCESS = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            close_fds=True,
        )
    except FileNotFoundError:
        log.error("ebusd binary not found at %s — bad image build", EBUSD_BIN)
        return
    except Exception as exc:
        log.error("ebusd failed to spawn: %s", exc)
        return
    threading.Thread(target=_ebusd_pump_logs, name="ebusd-logs", daemon=True).start()


def ebusd_watchdog() -> None:
    """Re-spawn ebusd if it dies. Capped restart rate (max 1 every 5 s)."""
    global EBUSD_PROCESS, EBUSD_LAST_RESTART, EBUSD_RESTART_COUNT
    proc = EBUSD_PROCESS
    if proc is None:
        ebusd_start()
        return
    rc = proc.poll()
    if rc is None:
        return  # still running
    now = time.time()
    if now - EBUSD_LAST_RESTART < 5:
        return
    EBUSD_LAST_RESTART = now
    EBUSD_RESTART_COUNT += 1
    log.warning("ebusd exited with rc=%s — restart #%d", rc, EBUSD_RESTART_COUNT)
    EBUSD_PROCESS = None
    ebusd_start()


def ebusd_stop() -> None:
    global EBUSD_PROCESS
    if EBUSD_PROCESS and EBUSD_PROCESS.poll() is None:
        log.info("Stopping ebusd (SIGTERM)")
        try:
            EBUSD_PROCESS.terminate()
            EBUSD_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("ebusd did not exit on SIGTERM, sending SIGKILL")
            EBUSD_PROCESS.kill()
        EBUSD_PROCESS = None


# ───────────────────────────────────────────────────────────────────────────
# Compatibility check — identify the connected hardware via ebusd's TCP
# command interface (port 8888, same protocol `ebusctl`/telnet use) and
# compare against the single unit this add-on has actually been tested on.
# ───────────────────────────────────────────────────────────────────────────

EBUSD_TCP_HOST = "127.0.0.1"
EBUSD_TCP_PORT = 8888

# The exact (and only) hardware this add-on has been validated against,
# taken verbatim from the header comments of the bundled CSVs in
# rootfs/usr/share/ebusd/vaillant/. Anything else *may* work — the aroTHERM
# family shares much of its eBUS schema — but is unverified.
KNOWN_GOOD_DEVICES = {
    "HMU00": {"manufacturer": "Vaillant", "sw_version": "0901", "hw_version": "5103",
              "circuit": "hmu", "role": "Heat Management Unit (outdoor unit ECU)"},
    "CTLS2": {"manufacturer": "Vaillant", "sw_version": "0509", "hw_version": "1304",
              "circuit": "ctls2", "role": "Sigma 2 room controller"},
    "VWZ":   {"manufacturer": "Vaillant", "sw_version": "0522", "hw_version": "5103",
              "circuit": "vwz", "role": "Compressor module (VWZ)"},
}


def _ebusd_tcp_command(cmd: str, timeout: float = 8.0, idle: float = 1.0) -> str:
    """Send one line to ebusd's TCP command interface and return the text
    response. Best-effort: returns "" on any connection/timeout error rather
    than raising, since this only backs an optional diagnostics feature.
    """
    import socket as _s
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    try:
        sock.connect((EBUSD_TCP_HOST, EBUSD_TCP_PORT))
        sock.sendall((cmd.strip() + "\n").encode("utf-8"))
        sock.settimeout(idle)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = sock.recv(4096)
            except _s.timeout:
                break
            if not data:
                break
            chunks.append(data)
    except OSError as exc:
        log.warning("ebusd TCP command %r failed: %s", cmd, exc)
    finally:
        sock.close()
    return b"".join(chunks).decode("utf-8", errors="replace")


def _parse_scan_result(text: str) -> list[dict]:
    """Parse `scan result` output into device dicts.

    Line format (per ebusd's own TCP-client docs), e.g.:
      08;Vaillant;EHP00;0327;7201;21;07;45;0010002779;0006;......;N8
      address;manufacturer;id;sw;hw;<extra columns we don't need>
    """
    devices = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(";")
        if len(parts) < 5 or not re.match(r"^[0-9a-fA-F]{2}$", parts[0]):
            continue
        devices.append({
            "address": parts[0],
            "manufacturer": parts[1],
            "id": parts[2],
            "sw_version": parts[3],
            "hw_version": parts[4],
        })
    return devices


def _message_schema_snapshot() -> list[dict]:
    """Field NAMES only (never live values) per (circuit, message) currently
    seen — the shape a new device-support CSV would need to match, without
    revealing anything about the household it was captured in."""
    with STATE_LOCK:
        return [
            {"circuit": circuit, "message": msg, "fields": sorted(entry["fields"].keys())}
            for (circuit, msg), entry in sorted(STATE.items())
        ]


def compat_check() -> dict:
    """Compare whatever ebusd has scanned on the bus against KNOWN_GOOD_DEVICES.

    Three buckets:
      matched             — id + sw + hw match the tested unit exactly
      mismatched_firmware — same product id, different sw/hw (probably fine,
                             unverified)
      unknown_device      — id we've never tested against at all
    """
    devices = _parse_scan_result(_ebusd_tcp_command("scan result"))
    matched, mismatched, unknown = [], [], []
    for dev in devices:
        ref = KNOWN_GOOD_DEVICES.get(dev["id"].upper())
        if ref is None:
            unknown.append(dev)
        elif dev["sw_version"] == ref["sw_version"] and dev["hw_version"] == ref["hw_version"]:
            matched.append(dev)
        else:
            mismatched.append(dev)
    return {
        "devices": devices,
        "matched": matched,
        "mismatched_firmware": mismatched,
        "unknown_device": unknown,
    }


def _install_signal_handlers() -> None:
    """Cleanly tear down ebusd when the container is stopped."""
    def _shutdown(signum, _frame):
        log.info("Received signal %s — shutting down", signum)
        ebusd_stop()
        SCHEDULER.shutdown(wait=False)
        sys.exit(0)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _shutdown)


# ───────────────────────────────────────────────────────────────────────────
# MQTT — subscribe to ebusd, drive STATE, write to /set, /get on demand
# ───────────────────────────────────────────────────────────────────────────

MQTT_CLIENT: mqtt.Client | None = None
MQTT_CONNECTED = threading.Event()


def _decode_payload(payload: bytes | str) -> tuple[dict, str]:
    raw = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"value": raw.strip('"')}, raw
    fields: dict = {}
    if isinstance(data, dict):
        for key, val in data.items():
            if isinstance(val, dict) and "value" in val:
                fields[key] = val["value"]
            else:
                fields[key] = val
    return fields, raw


def _on_mqtt_connect(client, _userdata, _flags, rc):
    if rc == 0:
        topic = f"{CONF['topic_prefix']}/+/+"
        client.subscribe(topic, qos=0)
        log.info("MQTT connected — subscribed to %s", topic)
        MQTT_CONNECTED.set()
    else:
        log.error("MQTT connect failed: rc=%s", rc)


def _on_mqtt_disconnect(_client, _userdata, rc):
    MQTT_CONNECTED.clear()
    log.warning("MQTT disconnected (rc=%s) — paho will reconnect", rc)


def _on_mqtt_message(_client, _userdata, msg):
    parts = msg.topic.split("/", 2)
    if len(parts) != 3 or parts[0] != CONF["topic_prefix"]:
        return
    _, circuit, message = parts
    if "/" in message or message in ("set", "get", "errors", "ebusd"):
        return
    fields, raw = _decode_payload(msg.payload)
    with STATE_LOCK:
        STATE[(circuit, message)] = {"fields": fields, "raw": raw, "ts": time.time()}


def mqtt_publish_write(circuit: str, msg: str, value) -> bool:
    """Publish a write command — the single choke point every write path in
    the addon goes through (manual API, optimizer, safety clamps, control-
    session snapshot restore), so validation lives here once rather than
    being re-implemented (or forgotten) at each call site.

    Returns False, and does NOT publish, if the value isn't safe to send
    (non-finite float — NaN/Infinity can reach here via a JSON payload,
    since Python's json module accepts NaN/Infinity as an extension) or if
    MQTT isn't actually connected right now. Never silently "succeeds" when
    it didn't: MQTT_CLIENT being non-None only means it exists, not that
    it's connected — a stale reference used to be enough to attempt a
    publish while disconnected."""
    if isinstance(value, float) and not math.isfinite(value):
        log.error("Refusing to write %s/%s: non-finite value %r", circuit, msg, value)
        return False
    if not MQTT_CLIENT or not MQTT_CONNECTED.is_set():
        log.error("Refusing to write %s/%s: MQTT not connected", circuit, msg)
        return False
    topic = f"{CONF['topic_prefix']}/{circuit}/{msg}/set"
    payload = str(value)
    log.info("MQTT write %s = %s", topic, payload)
    MQTT_CLIENT.publish(topic, payload, qos=0, retain=False)
    return True


def mqtt_request_read(circuit: str, msg: str) -> None:
    topic = f"{CONF['topic_prefix']}/{circuit}/{msg}/get"
    if MQTT_CLIENT:
        MQTT_CLIENT.publish(topic, "?", qos=0, retain=False)


def mqtt_start() -> None:
    global MQTT_CLIENT
    client = mqtt.Client(client_id=f"genia_air_addon_{int(time.time())}")
    if CONF["mqtt_user"]:
        client.username_pw_set(CONF["mqtt_user"], CONF["mqtt_pass"])
    client.on_connect = _on_mqtt_connect
    client.on_disconnect = _on_mqtt_disconnect
    client.on_message = _on_mqtt_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    MQTT_CLIENT = client
    try:
        client.connect_async(CONF["mqtt_host"], CONF["mqtt_port"], keepalive=60)
    except Exception as exc:
        log.error("MQTT initial connect_async failed: %s", exc)
    client.loop_start()


# ───────────────────────────────────────────────────────────────────────────
# Helpers — derived values from STATE
# ───────────────────────────────────────────────────────────────────────────

def _field(circuit: str, msg: str, key: str, cast=float):
    with STATE_LOCK:
        entry = STATE.get((circuit, msg))
    if not entry:
        return None
    val = entry["fields"].get(key)
    if val is None:
        return None
    try:
        return cast(val)
    except (TypeError, ValueError):
        return None


def compute_delta_t() -> float | None:
    supply = _field("hmu", "Status01", "0")
    ret    = _field("hmu", "Status01", "1")
    if supply is None or ret is None:
        return None
    return round(supply - ret, 2)


def compute_cop() -> float | None:
    """Instantaneous COP — None when the compressor is idle (p_in ≈ 0)."""
    p_in  = _field("hmu", "CurrentConsumedPower", "0")
    p_out = _field("hmu", "CurrentYieldPower", "0")
    if p_in is None or p_out is None or p_in <= 0.05:
        return None
    return round(p_out / p_in, 2)


def compute_cop_rolling(window_min: int = 30) -> float | None:
    """Rolling COP from the last `window_min` minutes of snapshots.

    Sum of thermal power / sum of electric power. Useful when the
    compressor is currently idle but ran recently. Returns None if there
    isn't enough data or no consumption was logged in the window.
    """
    in_rows  = db_query_series("power_in",  hours=max(1, (window_min + 59) // 60))
    out_rows = db_query_series("power_out", hours=max(1, (window_min + 59) // 60))
    since = int(time.time()) - window_min * 60
    in_sum  = sum(v for ts, v in in_rows  if ts >= since)
    out_sum = sum(v for ts, v in out_rows if ts >= since)
    if in_sum <= 0.05:
        return None
    return round(out_sum / in_sum, 2)


def _hours_field(circuit: str, msg: str) -> float | None:
    """ebusd v26 publishes Hours/HoursHc/HoursCool as {"energy": <N>} —
    older configs used positional "0". Try both."""
    v = _field(circuit, msg, "energy")
    if v is not None:
        return v
    return _field(circuit, msg, "0")


_STATE_TO_HVAC = {
    "0":   "idle",
    "1":   "idle",
    "9":   "heating",
    "17":  "cooling",
    "129": "heating",
    "11":  "off",
}


def compute_hvac_action() -> str:
    """What the pump is doing right now.

    Primary source is the hmu/State code (field 3). When that field is
    missing or carries a code we don't have mapped, infer the activity
    from the compressor: if it is drawing power or modulating, report
    heating/cooling (disambiguated by mode and the supply−return sign);
    otherwise idle. This avoids the opaque "unknown" pill in the UI.
    """
    with STATE_LOCK:
        entry = STATE.get(("hmu", "State"))
    if entry:
        mapped = _STATE_TO_HVAC.get(str(entry["fields"].get("3", "")))
        if mapped:
            return mapped
    modulation = _field("hmu", "CurrentCompressorUtil", "0")
    power_in = _field("hmu", "CurrentConsumedPower", "0")
    active = (modulation is not None and modulation > 1) or \
             (power_in is not None and power_in > 0.1)
    if not active:
        return "idle"
    mode = compute_hvac_mode()
    if mode == "cool":
        return "cooling"
    if mode == "heat":
        return "heating"
    # auto: the compressor is running but mode doesn't say which way —
    # use the flow vs return sign (negative ΔT = cooling).
    dt = compute_delta_t()
    if dt is not None and dt < -0.3:
        return "cooling"
    if dt is not None and dt > 0.3:
        return "heating"
    return "running"


def compute_setpoint_effective() -> float | None:
    """The target room temperature that actually applies right now.

    Picks the cooling or heating setpoint based on what the system is
    doing, so the UI never shows the (often 0.0 °C) heating-desired value
    while the unit is cooling in summer. Treats 0/None as "no target".
    """
    def ok(v):
        return v if (v is not None and v > 1) else None
    cooling = ok(_field("ctls2", "z1CoolingTemp", "tempv"))
    heating = ok(_field("ctls2", "z1ActualRoomTempDesired", "tempv")) \
        or ok(_field("ctls2", "z1ManualTemp", "tempv"))
    mode, action = compute_hvac_mode(), compute_hvac_action()
    if mode == "cool" or action == "cooling":
        return cooling or heating
    if mode == "heat" or action == "heating":
        return heating or cooling
    return heating or cooling


def compute_hvac_mode() -> str:
    sys_off = _field("ctls2", "GlobalSystemOff", "yesno", cast=str)
    if sys_off and sys_off.lower() == "yes":
        return "off"
    heat = _field("ctls2", "z1OpMode", "opmode", cast=str) or "off"
    cool = _field("ctls2", "z1OpModeCooling", "opmode", cast=str) or "off"
    h_on = heat.lower() in ("auto", "day", "night")
    c_on = cool.lower() in ("auto", "day", "night")
    if h_on and c_on:
        return "auto"
    if c_on:
        return "cool"
    if h_on:
        return "heat"
    return "off"


def collect_snapshot() -> dict:
    return {
        "ts": time.time(),
        "version": VERSION,
        "mqtt_connected": MQTT_CONNECTED.is_set(),
        "optimizer_enabled": OPTIMIZER_ENABLED,
        "hvac_mode": compute_hvac_mode(),
        "hvac_action": compute_hvac_action(),
        "room_temp": _field("ctls2", "z1RoomTemp", "tempv"),
        "setpoint_actual": _field("ctls2", "z1ActualRoomTempDesired", "tempv"),
        "setpoint_manual": _field("ctls2", "z1ManualTemp", "tempv"),
        "setpoint_cooling": _field("ctls2", "z1CoolingTemp", "tempv"),
        "setpoint_effective": compute_setpoint_effective(),
        "outside_temp": _field("ctls2", "OutsideTempAvg", "tempv"),
        "delta_t": compute_delta_t(),
        "cop": compute_cop(),
        "cop_rolling_30m": compute_cop_rolling(30),
        "supply_temp": _field("hmu", "Status01", "0"),
        "return_temp": _field("hmu", "Status01", "1"),
        "power_in": _field("hmu", "CurrentConsumedPower", "0"),
        "power_out": _field("hmu", "CurrentYieldPower", "0"),
        "compressor_modulation": _field("hmu", "CurrentCompressorUtil", "0"),
        "water_throughput": _field("hmu", "WaterThroughput", "0"),
        "hours_total": _hours_field("hmu", "Hours"),
        "hours_heating": _hours_field("hmu", "HoursHc"),
        "hours_cooling": _hours_field("hmu", "HoursCool"),
        "yield_total": _field("ctls2", "YieldTotal", "energy4"),
        "max_flow_temp": _field("ctls2", "Hc1MaxFlowTempDesired", "tempv"),
        "min_flow_temp": _field("ctls2", "Hc1MinFlowTempDesired", "tempv"),
        "summer_temp_limit": _field("ctls2", "Hc1SummerTempLimit", "tempv"),
        "continuous_heating": _field("ctls2", "ContinuosHeating", "tempv"),
        "maintenance_due": _field("ctls2", "MaintenanceDue", "yesno", cast=str),
        "opmode_heating": _field("ctls2", "z1OpMode", "opmode", cast=str),
        "opmode_cooling": _field("ctls2", "z1OpModeCooling", "opmode", cast=str),
        "sfmode": _field("ctls2", "z1SfMode", "sfmode", cast=str),
        "health": dict(HEALTH),
    }


# ───────────────────────────────────────────────────────────────────────────
# Scheduler — initial sync, snapshot, optimizer, health
# ───────────────────────────────────────────────────────────────────────────

SCHEDULER = BackgroundScheduler(timezone="UTC")


def task_initial_sync() -> None:
    """Force ebusd to read every msg we care about, paced so we don't queue-jam."""
    if not MQTT_CONNECTED.wait(timeout=10):
        log.warning("Initial sync: MQTT not ready, skip")
        return
    with STATE_LOCK:
        pending = [t for t in SUBSCRIBED_MSGS if t not in STATE]
    if not pending:
        log.info("Initial sync: STATE already populated (%d msgs)", len(SUBSCRIBED_MSGS))
        return
    log.info("Initial sync: requesting %d/%d msgs", len(pending), len(SUBSCRIBED_MSGS))
    for circuit, msg in pending:
        mqtt_request_read(circuit, msg)
        time.sleep(0.15)


def task_snapshot_history() -> None:
    """Persist a row per series with the current value."""
    snap = collect_snapshot()
    ts = int(snap["ts"])
    for series in (
        "room_temp", "setpoint_actual", "outside_temp",
        "supply_temp", "return_temp", "delta_t", "cop",
        "power_in", "power_out", "compressor_modulation",
        "water_throughput", "max_flow_temp",
    ):
        v = snap.get(series)
        if v is not None:
            db_insert_snapshot(ts, series, float(v))


WRITABLE_FIELD_KEY: dict[str, tuple[str, type]] = {
    "z1ManualTemp": ("tempv", float), "z1DayTemp": ("tempv", float),
    "z1NightTemp": ("tempv", float), "z1HolidayTemp": ("tempv", float),
    "z1CoolingTemp": ("tempv", float),
    "Hc1MaxFlowTempDesired": ("tempv", float), "Hc1MinFlowTempDesired": ("tempv", float),
    "Hc1SummerTempLimit": ("tempv", float), "ContinuosHeating": ("tempv", float),
    "z1OpMode": ("opmode", str), "z1OpModeCooling": ("opmode", str),
}


CONTROL_SNAPSHOT_PATH = DATA / "control_snapshot.json"


def control_is_active() -> bool:
    """True while a full-control session is live (enabled and not expired)."""
    with CONTROL_LOCK:
        if not CONTROL_ENABLED or CONTROL_EXPIRES_AT is None:
            return False
        return time.time() < CONTROL_EXPIRES_AT


def control_snapshot() -> dict:
    """Capture every writable value as it stands right now — the "initial
    state" a later revert restores to. Every WRITABLE_MSGS key is always
    present (value None if unknown yet, e.g. right after boot before the
    initial bus sync) so callers can see exactly what won't be restorable,
    rather than that gap being silently invisible."""
    snap = {}
    for circuit, msg in WRITABLE_MSGS:
        key, cast = WRITABLE_FIELD_KEY[msg]
        snap[msg] = {"circuit": circuit, "value": _field(circuit, msg, key, cast=cast)}
    return snap


def control_restore(snapshot: dict) -> None:
    """Re-publish every snapshotted value that was actually known — an actual
    restore, not just a stop. Fields whose value was None at snapshot time
    (never read from the bus) can't be restored to anything meaningful and
    are skipped."""
    for msg, entry in (snapshot or {}).items():
        if entry.get("value") is not None:
            mqtt_publish_write(entry["circuit"], msg, entry["value"])


def _save_control_snapshot(snapshot: dict | None) -> None:
    """Persist (or clear) the pre-session baseline to disk so a crash/restart
    mid-session doesn't lose it — the whole point of "revert restores the
    real values" falls apart if the only copy lives in a global that a
    restart wipes."""
    try:
        if snapshot:
            CONTROL_SNAPSHOT_PATH.write_text(json.dumps(snapshot))
        else:
            CONTROL_SNAPSHOT_PATH.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not persist control snapshot: %s", exc)


def control_recover_on_boot() -> None:
    """Called once at startup. A snapshot file left on disk means the addon
    was in full-control mode when it last stopped (crash, restart, update) —
    we can't know if anyone was still watching, so the safe move is to
    restore those values immediately and boot into read-only, same as any
    other auto-revert."""
    if not CONTROL_SNAPSHOT_PATH.exists():
        return
    try:
        snapshot = json.loads(CONTROL_SNAPSHOT_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read leftover control snapshot, discarding: %s", exc)
        CONTROL_SNAPSHOT_PATH.unlink(missing_ok=True)
        return
    log.warning("Found a control session snapshot from before restart — "
                "restoring %d value(s) and booting read-only", len(snapshot))
    control_restore(snapshot)
    _save_control_snapshot(None)
    db_log_decision("control_revert", "Sesión interrumpida por reinicio del addon",
                     {"restored_fields": len(snapshot)})
    _notify_ha(
        "Genia Air: sesión de control interrumpida",
        f"El addon se reinició mientras el control total estaba activo. Se han "
        f"restaurado {len(snapshot)} valor(es) previos y se ha vuelto a modo solo-lectura.",
    )


def control_enable(session_minutes: float | None = None) -> dict:
    """Start (or extend/renew) a full-control session. Snapshots current
    state only on first entry, so renewing an active session doesn't
    overwrite the original "before I touched anything" baseline."""
    global CONTROL_ENABLED, CONTROL_EXPIRES_AT, CONTROL_LAST_ACK_AT, CONTROL_SNAPSHOT
    minutes = session_minutes or CONF["control_session_min"]
    now = time.time()
    with CONTROL_LOCK:
        if not CONTROL_ENABLED:
            CONTROL_SNAPSHOT = control_snapshot()
            _save_control_snapshot(CONTROL_SNAPSHOT)
        CONTROL_ENABLED = True
        CONTROL_EXPIRES_AT = now + minutes * 60
        CONTROL_LAST_ACK_AT = now
        known = sum(1 for v in (CONTROL_SNAPSHOT or {}).values() if v["value"] is not None)
        total = len(CONTROL_SNAPSHOT or {})
        expires_at = CONTROL_EXPIRES_AT
    db_log_decision("control_enable", f"Full control enabled for {minutes:.0f}min",
                     {"minutes": minutes, "snapshot_known": known, "snapshot_total": total})
    log.warning("Full control ENABLED for %.0f minutes (baseline known for %d/%d field(s))",
                minutes, known, total)
    if known < total:
        log.warning("%d field(s) have no known value yet — they won't be restorable on revert "
                    "until the bus has reported them at least once", total - known)
    return {"expires_at": expires_at, "snapshot_known": known, "snapshot_total": total}


def control_ack() -> float:
    """User confirmed the current values still make sense — resets the
    unattended-for-too-long grace clock. Caller must check control_is_active()."""
    global CONTROL_LAST_ACK_AT
    with CONTROL_LOCK:
        CONTROL_LAST_ACK_AT = time.time()
        ack_at = CONTROL_LAST_ACK_AT
    db_log_decision("control_ack", "User confirmed control values", {})
    return ack_at


def _finish_revert(snapshot: dict | None, reason: str) -> None:
    """The I/O half of a revert (writes + logging + notify) — never called
    while holding CONTROL_LOCK, since these are network calls."""
    _save_control_snapshot(None)
    if snapshot:
        control_restore(snapshot)
    restored = sum(1 for v in (snapshot or {}).values() if v["value"] is not None)
    db_log_decision("control_revert", reason, {"restored_fields": restored})
    log.warning("Full control REVERTED (%s) — restored %d value(s)", reason, restored)
    _notify_ha(
        "Genia Air: control total desactivado",
        f"{reason}. Se ha vuelto a modo solo-lectura y se han restaurado los "
        f"valores previos a la sesión ({restored} campo(s)).",
    )


def control_revert(reason: str) -> None:
    """Explicit, unconditional end of the session (manual disable) — restore
    the pre-session snapshot and flip back to read-only."""
    global CONTROL_ENABLED, CONTROL_EXPIRES_AT, CONTROL_LAST_ACK_AT, CONTROL_SNAPSHOT
    with CONTROL_LOCK:
        snapshot, CONTROL_SNAPSHOT = CONTROL_SNAPSHOT, None
        CONTROL_ENABLED = False
        CONTROL_EXPIRES_AT = None
        CONTROL_LAST_ACK_AT = None
    _finish_revert(snapshot, reason)


def task_control_watchdog() -> None:
    """Runs every cycle: expire the session on hard timeout or stale ack.

    The decide-and-clear-state step happens in ONE critical section so a
    concurrent renewal (api_control_enable, e.g. the user re-confirming right
    at the boundary) can't race this: either the renewal lands first and this
    function then sees a fresh expiry/ack and does nothing, or this function
    clears state first and the renewal that follows correctly starts a brand
    new session — never "renew, then immediately get reverted anyway" using
    a decision made before the renewal happened.
    """
    global CONTROL_ENABLED, CONTROL_EXPIRES_AT, CONTROL_LAST_ACK_AT, CONTROL_SNAPSHOT
    with CONTROL_LOCK:
        if not CONTROL_ENABLED:
            return
        now = time.time()
        grace = CONF["control_ack_grace_min"] * 60
        expired = CONTROL_EXPIRES_AT is not None and now >= CONTROL_EXPIRES_AT
        stale = CONTROL_LAST_ACK_AT is not None and now - CONTROL_LAST_ACK_AT > grace
        # Fail-closed: cheap liveness checks only (no DB/collect_snapshot —
        # this runs every 30s and must stay fast). A broken ebusd/MQTT link
        # means we can't trust reads or reliably write anyway — end the
        # session now instead of waiting for its own timers to catch up.
        ebusd_dead = EBUSD_PROCESS is None or EBUSD_PROCESS.poll() is not None
        mqtt_down = not MQTT_CONNECTED.is_set()
        unhealthy = ebusd_dead or mqtt_down
        if not (expired or stale or unhealthy):
            return
        if unhealthy:
            reason = "Modo seguro: " + (", ".join(
                r for r, cond in (("ebusd caído", ebusd_dead), ("MQTT desconectado", mqtt_down)) if cond))
        else:
            reason = ("Sesión de control total caducada" if expired
                       else "No se confirmó a tiempo que los valores siguen teniendo sentido")
        snapshot, CONTROL_SNAPSHOT = CONTROL_SNAPSHOT, None
        CONTROL_ENABLED = False
        CONTROL_EXPIRES_AT = None
        CONTROL_LAST_ACK_AT = None
    _finish_revert(snapshot, reason)


def _notify_ha(title: str, message: str) -> None:
    """Best-effort HA notification via the Supervisor's Core API proxy — reuses
    SUPERVISOR_TOKEN (same one already used for MQTT credential lookup), no
    extra auth/config needed. Logs and gives up silently on failure: a
    notification failure must never be mistaken for a write failure."""
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or ""
    if not token:
        log.warning("No SUPERVISOR_TOKEN — cannot send HA notification: %s", title)
        return
    service = CONF["control_notify_target"] or "notify.notify"
    if "." not in service:
        service = f"notify.{service}"
    domain, _, svc = service.partition(".")
    try:
        r = requests.post(
            f"http://supervisor/core/api/services/{domain}/{svc}",
            headers={"Authorization": f"Bearer {token}"},
            json={"title": title, "message": message},
            timeout=8,
        )
        if r.status_code >= 300:
            log.warning("HA notify %s → HTTP %s: %s", service, r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("HA notify %s failed: %s", service, exc)


def task_optimize() -> None:
    """The actual control loop.

    Strategy v0.1 (deterministic only):
      * Weather-compensated max flow temp: derive a target from outdoor,
        clamp to [min_flow_temp_safe, max_flow_temp_safe], write only if
        the delta vs current is > 0.5 K (avoid chatter).
      * Summer/winter switchover.
      * Safety enforcement on min/max flow if the user manually set
        something unsafe.
      * Delta-T anomaly alerting (no actuation — see docs/PUMP-PWM.md).
    """
    if not OPTIMIZER_ENABLED:
        return
    # Full control gates every WRITE below, but not the ΔT anomaly check —
    # that's pure alerting (HEALTH/db_log_decision), no actuation, and must
    # keep working in the default read-only state or users lose monitoring
    # for as long as they haven't opted into full control.
    can_write = control_is_active()
    snap = collect_snapshot()
    actions: list[dict] = []

    if can_write:
        # --- safety enforcement on flow temps ---
        if snap["max_flow_temp"] is not None and snap["max_flow_temp"] > CONF["max_flow_temp_safe"] + 0.1:
            result = _force_write_safe(
                "ctls2", "Hc1MaxFlowTempDesired",
                CONF["max_flow_temp_safe"],
                f"max_flow_temp {snap['max_flow_temp']} > safe limit {CONF['max_flow_temp_safe']}",
                old_value=snap["max_flow_temp"],
            )
            if result:
                actions.append(result)
        if snap["min_flow_temp"] is not None and snap["min_flow_temp"] < CONF["min_flow_temp_safe"] - 0.1:
            result = _force_write_safe(
                "ctls2", "Hc1MinFlowTempDesired",
                CONF["min_flow_temp_safe"],
                f"min_flow_temp {snap['min_flow_temp']} < safe limit {CONF['min_flow_temp_safe']}",
                old_value=snap["min_flow_temp"],
            )
            if result:
                actions.append(result)

    # --- weather-compensated flow temp target ---
    out = snap["outside_temp"]
    out_valid = out is not None and math.isfinite(out)
    if can_write and out_valid and snap["hvac_mode"] == "heat":
        # Simple linear curve: at -10°C → 35; at +15°C → 25. Clamped.
        target = 30.0 - (out + 10) * (10.0 / 25.0)
        target = max(CONF["min_flow_temp_safe"] + 4, min(CONF["max_flow_temp_safe"], round(target, 1)))
        current = snap["max_flow_temp"]
        if current is not None and abs(target - current) >= 0.5:
            if mqtt_publish_write("ctls2", "Hc1MaxFlowTempDesired", target):
                db_log_decision(
                    "flow_curve",
                    f"max_flow_temp {current}→{target} based on outdoor {out:.1f}°C",
                    {"old_value": current, "new_value": target, "outside": out, "source": "optimizer"},
                )
                actions.append({"kind": "flow_curve", "to": target})

    # --- delta-T anomaly alert (no actuation — always runs, even read-only) ---
    dt = snap["delta_t"]
    if dt is not None and snap["hvac_action"] == "heating":
        if abs(dt - CONF["target_delta_t"]) > 0.8:
            HEALTH["ok"] = False
            reason = f"ΔT={dt} K off-target ({CONF['target_delta_t']} K ±0.8)"
            if reason not in HEALTH["reasons"]:
                HEALTH["reasons"].append(reason)
            db_log_decision("alert", reason, {"delta_t": dt, "target": CONF["target_delta_t"]})

    # --- summer/winter switchover ---
    if can_write and out_valid:
        if out > CONF["summer_temp_limit"] + 2 and snap["hvac_mode"] == "heat":
            ok1 = mqtt_publish_write("ctls2", "z1OpMode", "off")
            ok2 = mqtt_publish_write("ctls2", "z1OpModeCooling", "auto")
            if ok1 and ok2:
                actions.append({"kind": "season_switch", "to": "cool"})
                db_log_decision(
                    "season_switch",
                    f"outdoor {out:.1f}°C > summer limit + 2 → switch to cooling",
                    {"old_mode": "heat", "new_mode": "cool", "outside": out, "source": "optimizer"},
                )
        elif out < CONF["summer_temp_limit"] - 5 and snap["hvac_mode"] == "cool":
            ok1 = mqtt_publish_write("ctls2", "z1OpModeCooling", "off")
            ok2 = mqtt_publish_write("ctls2", "z1OpMode", "auto")
            if ok1 and ok2:
                actions.append({"kind": "season_switch", "to": "heat"})
                db_log_decision(
                    "season_switch",
                    f"outdoor {out:.1f}°C < summer limit - 5 → switch to heating",
                    {"old_mode": "cool", "new_mode": "heat", "outside": out, "source": "optimizer"},
                )

    log.info("optimize cycle: %d actions (writes %s)", len(actions),
              "enabled" if can_write else "read-only")


def _force_write_safe(circuit: str, msg: str, value, reason: str, old_value=None) -> dict | None:
    if not mqtt_publish_write(circuit, msg, value):
        db_log_decision("write_rejected", f"safety clamp rejected: {reason}",
                        {"circuit": circuit, "msg": msg, "value": value, "source": "safety"})
        return None
    db_log_decision("safety", reason,
                    {"circuit": circuit, "msg": msg, "old_value": old_value, "new_value": value,
                     "source": "safety"})
    return {"kind": "safety", "msg": msg, "to": value}


def task_health_check() -> None:
    snap = collect_snapshot()
    ok = True
    reasons: list[str] = []
    if EBUSD_PROCESS is None or EBUSD_PROCESS.poll() is not None:
        ok = False
        reasons.append("ebusd not running")
    if not snap["mqtt_connected"]:
        ok = False
        reasons.append("MQTT disconnected")
    if not STATE:
        ok = False
        reasons.append("No ebusd traffic received")
    if snap["maintenance_due"] and snap["maintenance_due"].lower() == "yes":
        ok = False
        reasons.append("Maintenance due")
    HEALTH["ok"] = ok
    HEALTH["reasons"] = reasons
    if not ok and snap["mqtt_connected"]:
        log.warning("Health: %s", reasons)


# ───────────────────────────────────────────────────────────────────────────
# MQTT Discovery — publish a small device into HA so automations can hook
# ───────────────────────────────────────────────────────────────────────────

DISCOVERY_SENT = False


def publish_ha_discovery() -> None:
    """Publish 6 entities into HA via discovery (one-shot, retained)."""
    global DISCOVERY_SENT
    if not MQTT_CLIENT or DISCOVERY_SENT:
        return
    device = {
        "identifiers": ["genia_air_addon"],
        "name": "Genia Air (addon)",
        "manufacturer": "Vaillant",
        "model": "Genia Air",
        "sw_version": VERSION,
    }
    base = "homeassistant"
    avail = "genia_air/addon/availability"
    entities = [
        {
            "kind": "sensor", "id": "state",
            "config": {"name": "State", "state_topic": "genia_air/addon/state",
                       "icon": "mdi:state-machine"},
        },
        {
            "kind": "sensor", "id": "cop",
            "config": {"name": "COP", "state_topic": "genia_air/addon/cop",
                       "icon": "mdi:gauge"},
        },
        {
            "kind": "sensor", "id": "delta_t",
            "config": {"name": "ΔT", "state_topic": "genia_air/addon/delta_t",
                       "unit_of_measurement": "K", "icon": "mdi:delta"},
        },
        {
            "kind": "binary_sensor", "id": "fault",
            "config": {"name": "Fault", "state_topic": "genia_air/addon/fault",
                       "device_class": "problem"},
        },
        {
            "kind": "switch", "id": "optimizer",
            "config": {"name": "Optimizer",
                       "state_topic": "genia_air/addon/optimizer/state",
                       "command_topic": "genia_air/addon/optimizer/set",
                       "payload_on": "ON", "payload_off": "OFF",
                       "icon": "mdi:brain"},
        },
    ]
    for ent in entities:
        kind, eid, conf = ent["kind"], ent["id"], dict(ent["config"])
        conf.update({
            "unique_id": f"genia_air_addon_{eid}",
            "availability_topic": avail,
            "device": device,
        })
        topic = f"{base}/{kind}/genia_air_addon/{eid}/config"
        MQTT_CLIENT.publish(topic, json.dumps(conf), qos=0, retain=True)
    MQTT_CLIENT.publish(avail, "online", retain=True)
    MQTT_CLIENT.publish("genia_air/addon/optimizer/state",
                        "ON" if OPTIMIZER_ENABLED else "OFF", retain=True)
    DISCOVERY_SENT = True
    log.info("MQTT Discovery published for HA (5 entities)")


def task_publish_states() -> None:
    """Push the derived values to the MQTT discovery topics."""
    if not MQTT_CLIENT or not MQTT_CONNECTED.is_set():
        return
    publish_ha_discovery()
    snap = collect_snapshot()
    MQTT_CLIENT.publish("genia_air/addon/state", snap["hvac_action"], retain=True)
    if snap["cop"] is not None:
        MQTT_CLIENT.publish("genia_air/addon/cop", snap["cop"], retain=True)
    if snap["delta_t"] is not None:
        MQTT_CLIENT.publish("genia_air/addon/delta_t", snap["delta_t"], retain=True)
    MQTT_CLIENT.publish("genia_air/addon/fault",
                        "ON" if not HEALTH["ok"] else "OFF", retain=True)
    MQTT_CLIENT.publish("genia_air/addon/optimizer/state",
                        "ON" if OPTIMIZER_ENABLED else "OFF", retain=True)


# Optimizer toggle handler via MQTT command topic
def _on_optimizer_command(_client, _userdata, msg):
    global OPTIMIZER_ENABLED
    val = msg.payload.decode("utf-8", errors="replace").strip().upper()
    OPTIMIZER_ENABLED = val == "ON"
    log.info("Optimizer toggled via HA: %s", OPTIMIZER_ENABLED)


def mqtt_post_connect_subs() -> None:
    """Subscribe to discovery command topics after connect."""
    if MQTT_CLIENT and MQTT_CONNECTED.is_set():
        MQTT_CLIENT.message_callback_add(
            "genia_air/addon/optimizer/set", _on_optimizer_command
        )
        MQTT_CLIENT.subscribe("genia_air/addon/optimizer/set", qos=0)


# ───────────────────────────────────────────────────────────────────────────
# Flask app — JSON API + embedded PANEL HTML
# ───────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


@app.before_request
def _ingress_guard():
    """All routes must arrive via HA Ingress (loopback allowed for healthchecks).

    HA Ingress only proxies *authenticated* HA sessions and stamps every
    request with X-Ingress-Path, so its presence already proves the caller
    is a logged-in HA user. Identity (for the audit log) comes from the
    X-Remote-User-* headers Ingress injects — note there is NO 'X-Hass-User'
    header; requiring it 403'd every write.
    """
    is_loopback = request.remote_addr in ("127.0.0.1", "::1")
    if request.path.startswith("/api/") or request.path == "/":
        if not request.headers.get("X-Ingress-Path") and not is_loopback:
            abort(403)


def _base_path() -> str:
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


@app.route("/")
def index():
    return PANEL.replace("__BASE__", _base_path())


@app.route("/api/state")
def api_state():
    return jsonify(collect_snapshot())


@app.route("/api/messages")
def api_messages():
    with STATE_LOCK:
        out = []
        for (circuit, msg), entry in sorted(STATE.items()):
            out.append({
                "circuit": circuit, "msg": msg,
                "fields": entry["fields"],
                "last_seen": entry["ts"],
                "age_seconds": int(time.time() - entry["ts"]),
            })
    return jsonify(out)


@app.route("/api/history")
def api_history():
    series = request.args.get("series", "")
    hours = int(request.args.get("hours", "24"))
    if not series:
        abort(400)
    rows = db_query_series(series, hours)
    return jsonify([{"ts": ts, "value": v} for ts, v in rows])


@app.route("/api/decisions")
def api_decisions():
    limit = int(request.args.get("limit", "50"))
    return jsonify(list(LAST_DECISIONS.values())[-limit:])


@app.route("/api/health")
def api_health():
    ebusd_alive = EBUSD_PROCESS is not None and EBUSD_PROCESS.poll() is None
    return jsonify({
        "ok": HEALTH["ok"],
        "reasons": HEALTH["reasons"],
        "since": HEALTH["since"],
        "mqtt_connected": MQTT_CONNECTED.is_set(),
        "ebusd_running": ebusd_alive,
        "ebusd_pid": EBUSD_PROCESS.pid if ebusd_alive else None,
        "ebusd_restarts": EBUSD_RESTART_COUNT,
        "state_size": len(STATE),
        "version": VERSION,
        "uptime_seconds": int(time.time() - HEALTH["since"]),
    })


@app.route("/api/ebusd", methods=["POST"])
def api_ebusd_action():
    """Manual control: {action: 'restart'|'stop'|'start'}."""
    action = (request.get_json(force=True, silent=True) or {}).get("action", "")
    if action == "restart":
        ebusd_stop()
        time.sleep(1)
        ebusd_start()
    elif action == "stop":
        ebusd_stop()
    elif action == "start":
        ebusd_start()
    else:
        abort(400)
    db_log_decision("user_ebusd", f"ebusd {action}", {"action": action})
    return jsonify({"ok": True, "action": action,
                    "running": EBUSD_PROCESS is not None and EBUSD_PROCESS.poll() is None})


def _old_value_for(circuit: str, msg: str):
    """Best-effort read of the current value of a writable field, for audit
    logging — None if it's not one we track (unknown msg) or never read."""
    key_cast = WRITABLE_FIELD_KEY.get(msg)
    if not key_cast:
        return None
    key, cast = key_cast
    return _field(circuit, msg, key, cast=cast)


@app.route("/api/write", methods=["POST"])
def api_write():
    if not control_is_active():
        abort(403)
    body = request.get_json(force=True, silent=True) or {}
    circuit = body.get("circuit")
    msg = body.get("msg")
    value = body.get("value")
    if not circuit or not msg or value is None:
        abort(400)
    # Safety clamp on known flow-temp keys
    if msg == "Hc1MaxFlowTempDesired":
        value = max(CONF["min_flow_temp_safe"] + 4, min(CONF["max_flow_temp_safe"], float(value)))
    if msg == "Hc1MinFlowTempDesired":
        value = max(CONF["min_flow_temp_safe"], min(CONF["max_flow_temp_safe"] - 4, float(value)))
    old_value = _old_value_for(circuit, msg)
    user = (request.headers.get("X-Remote-User-Name")
            or request.headers.get("X-Remote-User-Id", "unknown"))
    if not mqtt_publish_write(circuit, msg, value):
        db_log_decision("write_rejected", f"{circuit}/{msg}={value} rejected (invalid value or MQTT down)",
                        {"circuit": circuit, "msg": msg, "value": value, "source": "manual", "user": user})
        abort(503)
    db_log_decision("user_write", f"{circuit}/{msg}: {old_value} → {value} by user {user[:8]}",
                    {"circuit": circuit, "msg": msg, "old_value": old_value, "new_value": value,
                     "source": "manual", "user": user})
    return jsonify({"ok": True, "circuit": circuit, "msg": msg, "value": value})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    if not control_is_active():
        abort(403)
    mode = (request.get_json(force=True, silent=True) or {}).get("mode", "")
    if mode not in ("off", "heat", "cool", "auto"):
        abort(400)
    old_mode = compute_hvac_mode()
    pairs = {
        "off":  [("z1OpMode", "off"),  ("z1OpModeCooling", "off")],
        "heat": [("z1OpMode", "auto"), ("z1OpModeCooling", "off")],
        "cool": [("z1OpMode", "off"),  ("z1OpModeCooling", "auto")],
        "auto": [("z1OpMode", "auto"), ("z1OpModeCooling", "auto")],
    }[mode]
    if not all(mqtt_publish_write("ctls2", msg, val) for msg, val in pairs):
        db_log_decision("write_rejected", f"HVAC mode → {mode} rejected (MQTT down)",
                        {"mode": mode, "source": "manual"})
        abort(503)
    db_log_decision("user_mode", f"HVAC mode: {old_mode} → {mode}",
                    {"old_mode": old_mode, "new_mode": mode, "source": "manual"})
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/setpoint", methods=["POST"])
def api_setpoint():
    if not control_is_active():
        abort(403)
    body = request.get_json(force=True, silent=True) or {}
    target = float(body.get("target_c", 0))
    if not (5 <= target <= 30):
        abort(400)
    hvac = compute_hvac_mode()
    msg = "z1CoolingTemp" if hvac == "cool" else "z1ManualTemp"
    old_value = _old_value_for("ctls2", msg)
    if not mqtt_publish_write("ctls2", msg, target):
        db_log_decision("write_rejected", f"{msg} = {target}°C rejected (MQTT down)",
                        {"msg": msg, "value": target, "source": "manual"})
        abort(503)
    db_log_decision("user_setpoint", f"{msg}: {old_value} → {target}°C",
                    {"msg": msg, "old_value": old_value, "new_value": target, "source": "manual"})
    return jsonify({"ok": True, "msg": msg, "value": target})


@app.route("/api/optimizer", methods=["POST"])
def api_optimizer():
    global OPTIMIZER_ENABLED
    body = request.get_json(force=True, silent=True) or {}
    OPTIMIZER_ENABLED = bool(body.get("enable", True))
    db_log_decision("user_optimizer", f"Optimizer = {OPTIMIZER_ENABLED}",
                    {"enabled": OPTIMIZER_ENABLED})
    return jsonify({"ok": True, "enabled": OPTIMIZER_ENABLED})


@app.route("/api/control/status", methods=["GET"])
def api_control_status():
    active = control_is_active()
    return jsonify({
        "active": active,
        "expires_at": CONTROL_EXPIRES_AT if active else None,
        "last_ack_at": CONTROL_LAST_ACK_AT if active else None,
        "ack_grace_minutes": CONF["control_ack_grace_min"],
        "session_minutes": CONF["control_session_min"],
    })


@app.route("/api/control/enable", methods=["POST"])
def api_control_enable():
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("confirm"):
        # Require the explicit "I understand" flag from the UI — this is not
        # a UX nicety, it's the actual gate: no route accepts a bare enable.
        abort(400)
    user = (request.headers.get("X-Remote-User-Name")
            or request.headers.get("X-Remote-User-Id", "unknown"))
    result = control_enable()
    log.warning("Full control enabled by user %s", user[:8])
    return jsonify({"ok": True, **result})


@app.route("/api/control/disable", methods=["POST"])
def api_control_disable():
    if control_is_active():
        control_revert("Control total desactivado manualmente por el usuario")
    return jsonify({"ok": True})


@app.route("/api/control/ack", methods=["POST"])
def api_control_ack():
    if not control_is_active():
        abort(409)
    return jsonify({"ok": True, "last_ack_at": control_ack()})


@app.route("/api/force_read", methods=["POST"])
def api_force_read():
    """Trigger initial_sync on demand."""
    threading.Thread(target=task_initial_sync, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/ebusd_scan", methods=["POST"])
def api_ebusd_scan():
    """Trigger a fresh full bus scan in the background (can take a while)."""
    threading.Thread(
        target=lambda: _ebusd_tcp_command("scan full", timeout=90, idle=5),
        daemon=True,
    ).start()
    db_log_decision("user_scan", "Full eBUS scan requested", {})
    return jsonify({"ok": True})


@app.route("/api/compat_report")
def api_compat_report():
    """Anonymous compatibility report: protocol-level device identification
    and message/field NAMES only — no live sensor values, no network
    addresses, no credentials. Safe to attach to a public GitHub issue."""
    compat = compat_check()
    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "addon_version": VERSION,
        "tested_reference_hardware": KNOWN_GOOD_DEVICES,
        "detected_devices": compat["devices"],
        "compatibility": {
            "matched": compat["matched"],
            "mismatched_firmware": compat["mismatched_firmware"],
            "unknown_device": compat["unknown_device"],
        },
        "message_schema": _message_schema_snapshot(),
        "note": (
            "Contains only eBUS protocol identifiers (bus address, "
            "manufacturer, product/firmware IDs) and message/field NAMES — "
            "no sensor readings, IPs or credentials. Safe to attach to a "
            "public GitHub issue: "
            "https://github.com/onemanfoundry/ha-genia-air/issues/new?"
            "template=device-support.yml"
        ),
    }
    resp = jsonify(report)
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="genia_air_compat_report_{int(time.time())}.json"'
    )
    return resp


@app.route("/api/config")
def api_config():
    out = dict(CONF)
    out.pop("mqtt_pass", None)
    return jsonify(out)


# ───────────────────────────────────────────────────────────────────────────
# Embedded UI — single HTML string (PANEL) served from /
# ───────────────────────────────────────────────────────────────────────────

PANEL = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Genia Air</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
:root{--bg:#0f172a;--s:#1e293b;--b:#334155;--a:#38bdf8;--g:#4ade80;--y:#fbbf24;--r:#f87171;--o:#fb923c;--t:#e2e8f0;--m:#94a3b8;--p:#a78bfa}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--t);font-family:system-ui,sans-serif;padding:1rem;font-size:14px}
h1{color:var(--a);font-size:1.3rem;margin-bottom:.8rem;display:flex;align-items:center;gap:.5rem}
h2{font-size:.7rem;color:var(--m);text-transform:uppercase;letter-spacing:.08em;margin:.8rem 0 .5rem}
.tabs{display:flex;gap:.25rem;margin-bottom:1rem;border-bottom:1px solid var(--b);padding-bottom:.5rem;flex-wrap:wrap}
.tab{background:transparent;border:none;color:var(--m);padding:.4rem .9rem;border-radius:.4rem;cursor:pointer;font-size:.8rem;font-weight:600;transition:.15s}
.tab:hover{color:var(--t);background:rgba(255,255,255,.05)}
.tab.active{color:var(--a);background:rgba(56,189,248,.1)}
.tab-content{display:none}
.tab-content.active{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.6rem;margin-bottom:1rem}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;margin-bottom:1rem}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.card{background:var(--s);border-radius:.75rem;padding:.9rem;border:1px solid var(--b)}
.chart-card{display:flex;flex-direction:column}
.chart-wrap{position:relative;height:240px;width:100%}
.metric{font-size:1.8rem;font-weight:700;color:var(--a);line-height:1}
.metric.g{color:var(--g)}.metric.y{color:var(--y)}.metric.r{color:var(--r)}.metric.o{color:var(--o)}.metric.p{color:var(--p)}
.label{font-size:.72rem;color:var(--m);margin-top:.3rem}
.sub{font-size:.78rem;color:var(--m);margin-top:.15rem}
.badge{display:inline-block;padding:.15rem .5rem;border-radius:.3rem;font-size:.72rem;font-weight:600}
.bg-r{color:var(--r);background:rgba(248,113,113,.12)}
.bg-g{color:var(--g);background:rgba(74,222,128,.12)}
.bg-y{color:var(--y);background:rgba(251,191,36,.12)}
.bg-a{color:var(--a);background:rgba(56,189,248,.12)}
.bg-m{color:var(--m);background:rgba(148,163,184,.12)}
.btn{background:var(--a);color:#0f172a;border:none;padding:.45rem 1rem;border-radius:.5rem;font-weight:700;cursor:pointer;font-size:.8rem;transition:.15s opacity}
.btn:hover{opacity:.85}.btn:disabled{opacity:.4;cursor:default}
.btn-y{background:var(--y)}.btn-g{background:var(--g)}.btn-r{background:var(--r)}.btn-p{background:var(--p)}.btn-sm{padding:.3rem .7rem;font-size:.72rem}
.actions{display:flex;gap:.5rem;margin-bottom:1rem;flex-wrap:wrap;align-items:center}
.thermo{background:var(--s);border-radius:.75rem;padding:1.1rem;border:1px solid var(--b);margin-bottom:1rem}
.thermo-row{display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.8rem}
.thermo-temp{font-size:3rem;font-weight:700;color:var(--a);line-height:1}
.thermo-meta{display:flex;flex-direction:column;gap:.3rem;flex:1;min-width:160px}
.thermo-line{font-size:.78rem;color:var(--m)}
.thermo-line span{color:var(--t);font-weight:600}
.mode-pill{display:inline-flex;align-items:center;gap:.3rem;padding:.3rem .65rem;border-radius:999px;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
.action-pill{display:inline-block;padding:.15rem .55rem;border-radius:.3rem;font-size:.7rem;font-weight:700;text-transform:uppercase}
.setp-row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.setp-row input[type=number]{background:var(--b);border:1px solid #475569;border-radius:.4rem;padding:.4rem .55rem;color:var(--t);width:80px;font-size:.85rem;font-weight:600}
.range-row{padding:.55rem 0;border-bottom:1px solid rgba(51,65,85,.5)}
.range-row:last-child{border-bottom:none}
.range-h{display:flex;justify-content:space-between;margin-bottom:.3rem}
.range-name{font-size:.82rem;color:var(--t)}
.range-val{font-size:.82rem;color:var(--a);font-weight:700}
input[type=range]{width:100%;accent-color:var(--a);cursor:pointer}
select{background:var(--b);border:1px solid #475569;color:var(--t);border-radius:.4rem;padding:.4rem .55rem;font-size:.82rem;font-weight:600}
.toast{position:fixed;bottom:1.2rem;right:1.2rem;padding:.6rem 1.2rem;border-radius:.5rem;font-weight:600;font-size:.85rem;opacity:0;transition:.25s opacity;pointer-events:none;z-index:999;max-width:340px}
.toast.show{opacity:1}
.toast.ok{background:rgba(74,222,128,.95);color:#0f172a}
.toast.err{background:rgba(248,113,113,.95);color:#0f172a}
.toast.info{background:rgba(56,189,248,.95);color:#0f172a}
table{width:100%;border-collapse:collapse;font-size:.78rem}
td,th{padding:.4rem .5rem;border-bottom:1px solid var(--b);text-align:left}
th{color:var(--m);font-weight:500}
.diag-old{color:var(--y)}.diag-stale{color:var(--r)}.diag-fresh{color:var(--g)}
.dec-time{color:var(--m);font-size:.7rem;white-space:nowrap}
.health-card{padding:.8rem 1rem;border-radius:.6rem;margin-bottom:1rem;font-size:.85rem;border:1px solid}
.health-card.ok{background:rgba(74,222,128,.07);border-color:rgba(74,222,128,.3);color:var(--g)}
.health-card.warn{background:rgba(248,113,113,.07);border-color:rgba(248,113,113,.3);color:var(--r)}
.toggle{position:relative;display:inline-block;width:42px;height:22px;vertical-align:middle}
.toggle input{opacity:0;width:0;height:0}
.tslide{position:absolute;cursor:pointer;inset:0;background:var(--b);border-radius:22px;transition:.2s}
.tslide:before{position:absolute;content:"";height:16px;width:16px;left:3px;bottom:3px;background:var(--m);border-radius:50%;transition:.2s}
input:checked+.tslide{background:var(--a)}
input:checked+.tslide:before{transform:translateX(20px);background:#0f172a}
.empty{text-align:center;padding:2rem 1rem;color:var(--m);font-size:.85rem}
</style>
</head>
<body>
<h1>🌡️ Genia Air <span id="version" style="font-size:.7rem;color:var(--m);font-weight:400">v...</span></h1>
<div id="toast" class="toast"></div>
<div class="tabs">
  <button class="tab active" data-tab="overview">📊 Overview</button>
  <button class="tab" data-tab="charts">📈 Charts</button>
  <button class="tab" data-tab="controls">🎛️ Controls</button>
  <button class="tab" data-tab="optimizer">🧠 Optimizer</button>
  <button class="tab" data-tab="diag">🔧 Diagnostics</button>
</div>

<!-- Overview -->
<div id="t-overview" class="tab-content active">
  <div id="health-banner"></div>
  <div class="thermo">
    <h2 style="margin-top:0">Zone 1 thermostat</h2>
    <div class="thermo-row">
      <div>
        <div class="thermo-temp" id="room-temp">--</div>
        <div class="label">Room temperature</div>
      </div>
      <div class="thermo-meta">
        <div class="thermo-line">Target temperature: <span id="setp-actual" style="font-size:1.1rem;color:var(--a);font-weight:700">--</span> <span id="setp-which" class="badge bg-m" style="display:none"></span></div>
        <div class="thermo-line">Mode: <span id="hvac-mode" class="mode-pill bg-m">--</span> <span id="hvac-action" class="action-pill bg-m">--</span></div>
        <div class="thermo-line">Outdoor (averaged): <span id="outside">--</span></div>
      </div>
    </div>
    <div class="setp-row">
      <span class="label" style="margin:0">Change setpoint:</span>
      <input id="setp-input" type="number" step="0.5" min="12" max="28">
      <button class="btn btn-sm" onclick="setSetpoint()">Apply</button>
    </div>
  </div>
  <h2>System</h2>
  <div id="kpis" class="grid"></div>
  <h2>Optimizer</h2>
  <div id="opt-status" class="card" style="font-size:.85rem"></div>
</div>

<!-- Charts -->
<div id="t-charts" class="tab-content">
  <div class="actions">
    <span class="label" style="margin:0">Window:</span>
    <select id="chart-window">
      <option value="6">6 h</option>
      <option value="24" selected>24 h</option>
      <option value="72">72 h</option>
      <option value="168">7 days</option>
    </select>
    <button class="btn btn-sm" onclick="loadCharts()">🔄 Refresh</button>
  </div>
  <div class="grid2">
    <div class="card chart-card"><h2 style="margin-top:0">Temperatures (°C)</h2><div class="chart-wrap"><canvas id="ch-temps"></canvas></div></div>
    <div class="card chart-card"><h2 style="margin-top:0">ΔT supply − return (K)</h2><div class="chart-wrap"><canvas id="ch-dt"></canvas></div></div>
  </div>
  <div class="grid2">
    <div class="card chart-card"><h2 style="margin-top:0">Electric vs thermal power (kW)</h2><div class="chart-wrap"><canvas id="ch-pow"></canvas></div></div>
    <div class="card chart-card"><h2 style="margin-top:0">COP (instantaneous)</h2><div class="chart-wrap"><canvas id="ch-cop"></canvas></div></div>
  </div>
</div>

<!-- Controls -->
<div id="t-controls" class="tab-content">
  <div class="card" id="control-gate-card">
    <div id="control-readonly" style="display:none">
      <h2 style="margin-top:0">🔒 Solo lectura</h2>
      <p class="sub">Los controles de abajo están desactivados. Nada se escribe en la caldera
        hasta que actives el control total explícitamente, y esa sesión caduca sola.</p>
      <label style="display:flex;gap:.5rem;align-items:flex-start;margin:.75rem 0">
        <input type="checkbox" id="control-confirm-chk" style="margin-top:.2rem">
        <span>Entiendo que esto va a controlar mi calefacción/refrigeración real, y me
          comprometo a revisar que los valores tengan sentido cuando me lo pida la app.
          Si no confirmo a tiempo, se revierte sola.</span>
      </label>
      <button class="btn btn-sm btn-g" id="control-enable-btn" onclick="enableControl()">
        🔓 Activar control total</button>
    </div>
    <div id="control-active" style="display:none">
      <h2 style="margin-top:0">🔓 Control total activo</h2>
      <p class="sub">Caduca a las <span id="control-expires"></span> ·
        última confirmación: <span id="control-last-ack"></span></p>
      <div class="actions">
        <button class="btn btn-sm btn-g" onclick="ackControl()">✅ Los valores tienen sentido</button>
        <button class="btn btn-sm" onclick="disableControl()">🔒 Volver a solo lectura</button>
      </div>
    </div>
  </div>
  <div class="card">
    <h2 style="margin-top:0">HVAC mode</h2>
    <div class="actions">
      <button class="btn btn-sm" onclick="setMode('off')">⏻ Off</button>
      <button class="btn btn-sm btn-g" onclick="setMode('heat')">🔥 Heat</button>
      <button class="btn btn-sm btn-p" onclick="setMode('cool')">❄ Cool</button>
      <button class="btn btn-sm btn-y" onclick="setMode('auto')">🔄 Auto</button>
    </div>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Zone setpoints</h2>
    <div id="setp-sliders"></div>
  </div>
  <div class="card">
    <h2 style="margin-top:0">Flow curve &amp; safety limits</h2>
    <div id="flow-sliders"></div>
  </div>
</div>

<!-- Optimizer -->
<div id="t-optimizer" class="tab-content">
  <div class="card" style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap">
    <label class="toggle"><input type="checkbox" id="opt-toggle" onchange="toggleOptimizer()"><span class="tslide"></span></label>
    <div>
      <div style="font-weight:600">Active control</div>
      <div class="sub" style="margin-top:.2rem">Dynamic flow curve, seasonal switchover, safety enforcement</div>
    </div>
  </div>
  <h2>Recent decisions</h2>
  <div class="card" style="overflow-x:auto">
    <table><thead><tr><th>Time</th><th>Type</th><th>Reason</th></tr></thead><tbody id="dec-tbody"></tbody></table>
  </div>
</div>

<!-- Diagnostics -->
<div id="t-diag" class="tab-content">
  <div id="compat-card" class="card" style="margin-bottom:1rem"></div>
  <div id="ebusd-card" class="card" style="margin-bottom:1rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap"></div>
  <div class="actions">
    <button class="btn btn-sm" onclick="forceRead()">📡 Force-read all</button>
    <button class="btn btn-sm btn-y" onclick="ebusdAction('restart')">🔁 Restart ebusd</button>
    <button class="btn btn-sm" onclick="rescanBus()">🔍 Re-scan bus</button>
    <button class="btn btn-sm btn-g" onclick="reportToGithub()">🐙 Report on GitHub</button>
    <a class="btn btn-sm btn-p" id="compat-dl" href="#" target="_blank" style="text-decoration:none;display:inline-block">⬇ Download JSON</a>
    <button class="btn btn-sm" onclick="loadDiag()">🔄 Refresh</button>
  </div>
  <div class="card" style="overflow-x:auto">
    <table><thead><tr><th>Circuit</th><th>Message</th><th>Fields</th><th>Age</th></tr></thead><tbody id="diag-tbody"></tbody></table>
  </div>
</div>

<script>
const BASE = "__BASE__";
const $ = id => document.getElementById(id);
const fmt = (v, suf="", d=1) => (v==null||isNaN(v)) ? "—" : (typeof v==="number" ? v.toFixed(d) : v)+suf;
const fmtAge = s => s<60?s+"s":s<3600?Math.floor(s/60)+"m":Math.floor(s/3600)+"h";
const fmtTime = ts => new Date(ts*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});

function toast(msg, kind="info"){
  const t=$("toast"); t.textContent=msg; t.className="toast "+kind+" show";
  setTimeout(()=>t.className="toast",2400);
}
async function api(path, opts={}){
  opts.headers=Object.assign({"Content-Type":"application/json"}, opts.headers||{});
  const r=await fetch(BASE+path, opts);
  if(!r.ok) throw new Error("HTTP "+r.status);
  return r.json();
}

// Tab switching
document.querySelectorAll(".tab").forEach(b=>b.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".tab-content").forEach(x=>x.classList.remove("active"));
  b.classList.add("active");
  $("t-"+b.dataset.tab).classList.add("active");
  if(b.dataset.tab==="charts") loadCharts();
  if(b.dataset.tab==="diag") loadDiag();
  if(b.dataset.tab==="controls") { buildSliders(); loadControlStatus(); }
  if(b.dataset.tab==="optimizer") loadDecisions();
});

// Overview
const MODE_CLASS = {heat:"bg-g",cool:"bg-a",auto:"bg-y",off:"bg-m",unknown:"bg-m"};
const ACTION_CLASS = {heating:"bg-g",cooling:"bg-a",idle:"bg-m",off:"bg-m",running:"bg-y",unknown:"bg-m"};

async function loadState(){
  try{
    const s = await api("/api/state");
    $("version").textContent = "v"+s.version;
    $("room-temp").textContent = fmt(s.room_temp, "°C");
    const target = (s.setpoint_effective!=null) ? s.setpoint_effective : s.setpoint_actual;
    $("setp-actual").textContent = fmt(target, "°C");
    // Tell the user whether the shown target is the heating or cooling one.
    const which = (s.hvac_mode==="cool"||s.hvac_action==="cooling") ? "cooling"
                : (s.hvac_mode==="heat"||s.hvac_action==="heating") ? "heating" : "";
    const sw = $("setp-which");
    if(which){ sw.textContent = which; sw.style.display=""; sw.className="badge "+(which==="cooling"?"bg-a":"bg-g"); }
    else { sw.style.display="none"; }
    $("hvac-mode").textContent = s.hvac_mode.toUpperCase();
    $("hvac-mode").className = "mode-pill "+(MODE_CLASS[s.hvac_mode]||"bg-m");
    // Hide the activity pill entirely when we genuinely can't tell, rather
    // than showing a confusing "UNKNOWN".
    const ha = $("hvac-action");
    if(s.hvac_action && s.hvac_action!=="unknown"){
      ha.textContent = s.hvac_action.toUpperCase();
      ha.className = "action-pill "+(ACTION_CLASS[s.hvac_action]||"bg-m");
      ha.style.display="";
    } else { ha.style.display="none"; }
    $("outside").textContent = fmt(s.outside_temp, "°C");
    if(!$("setp-input").dataset.touched) $("setp-input").value = target || s.setpoint_manual || "";
    const k = $("kpis"); k.innerHTML="";
    [
      ["ΔT", fmt(s.delta_t, " K"), s.delta_t==null?"":"y"],
      // Instantaneous COP when running; otherwise the 30-min rolling avg with
      // a small badge so the user knows it's not live.
      s.cop != null
        ? ["COP", fmt(s.cop, "", 2), s.cop>3?"g":s.cop>2?"y":"r"]
        : (s.cop_rolling_30m != null
            ? ["COP (30 min)", fmt(s.cop_rolling_30m, "", 2),
               s.cop_rolling_30m>3?"g":s.cop_rolling_30m>2?"y":"r"]
            : ["COP", "idle", "m"]),
      ["Modulation", fmt(s.compressor_modulation, " %", 0), "p"],
      ["Electric power", fmt(s.power_in, " kW", 2), "y"],
      ["Thermal power", fmt(s.power_out, " kW", 2), "g"],
      ["Flow rate", fmt(s.water_throughput, " L/h", 0), "a"],
      ["Supply temp", fmt(s.supply_temp, "°C"), "o"],
      ["Return temp", fmt(s.return_temp, "°C"), "a"],
      ["Total hours", fmt(s.hours_total, " h", 0), ""],
    ].forEach(([lbl,val,kind])=>{
      const div=document.createElement("div"); div.className="card";
      div.innerHTML=`<div class="metric ${kind||""}">${val}</div><div class="label">${lbl}</div>`;
      k.appendChild(div);
    });
    const hb = $("health-banner");
    if(!s.health.ok && s.health.reasons.length){
      hb.className="health-card warn";
      hb.innerHTML="⚠ "+s.health.reasons.map(r=>`<div>${r}</div>`).join("");
    } else {
      hb.className="health-card ok";
      hb.innerHTML="✓ System healthy · MQTT "+(s.mqtt_connected?"connected":"disconnected");
    }
    $("opt-status").innerHTML =
      `<div>Active control: <span class="badge ${s.optimizer_enabled?'bg-g':'bg-m'}">${s.optimizer_enabled?'ON':'OFF'}</span></div>`+
      `<div class="sub" style="margin-top:.3rem">Dynamic flow-temperature curve, seasonal switchover and safety enforcement. See Optimizer tab for the decision log.</div>`;
    $("opt-toggle").checked = !!s.optimizer_enabled;
  } catch(e){ toast("State load failed: "+e.message, "err"); }
}

async function setSetpoint(){
  const v = parseFloat($("setp-input").value);
  if(isNaN(v)) return toast("Invalid setpoint","err");
  try{
    const r = await api("/api/setpoint", {method:"POST", body:JSON.stringify({target_c:v})});
    toast("Setpoint → "+r.value+"°C", "ok");
    $("setp-input").dataset.touched="";
    setTimeout(loadState, 800);
  } catch(e){ toast("Error: "+e.message,"err"); }
}
$("setp-input").addEventListener("input", e=>e.target.dataset.touched="1");

async function setMode(mode){
  try{ await api("/api/mode", {method:"POST", body:JSON.stringify({mode})});
       toast("Mode → "+mode.toUpperCase(),"ok"); setTimeout(loadState,800);
  } catch(e){ toast("Error: "+e.message,"err"); }
}

// Controls — dynamic sliders
const SETPOINT_DEFS = [
  ["z1ManualTemp",  "Manual",       12, 28, 0.5],
  ["z1DayTemp",     "Day",          14, 26, 0.5],
  ["z1NightTemp",   "Night",        16, 26, 0.5],
  ["z1HolidayTemp", "Holiday",       5, 22, 0.5],
  ["z1CoolingTemp", "Cooling",      16, 26, 0.5],
];
const FLOW_DEFS = [
  ["Hc1MaxFlowTempDesired", "Max flow temp",            25, 40, 0.5],
  ["Hc1MinFlowTempDesired", "Min flow temp",            14, 30, 0.5],
  ["Hc1SummerTempLimit",    "Summer temp limit",        12, 28, 0.5],
  ["ContinuosHeating",      "Continuous heating temp",  -26, 15, 0.5],
];
async function buildSliders(){
  const s = await api("/api/state");
  const fieldFor = {z1ManualTemp:"setpoint_manual", z1DayTemp:null, z1NightTemp:null,
                    z1HolidayTemp:null, z1CoolingTemp:"setpoint_cooling",
                    Hc1MaxFlowTempDesired:"max_flow_temp",
                    Hc1MinFlowTempDesired:"min_flow_temp",
                    Hc1SummerTempLimit:"summer_temp_limit",
                    ContinuosHeating:"continuous_heating"};
  function render(target, defs){
    target.innerHTML="";
    defs.forEach(([msg, name, lo, hi, step])=>{
      const cur = fieldFor[msg] ? s[fieldFor[msg]] : null;
      const val = cur==null ? (lo+hi)/2 : cur;
      const div = document.createElement("div"); div.className="range-row";
      div.innerHTML = `<div class="range-h"><span class="range-name">${name}</span><span class="range-val" id="rv-${msg}">${val}°C</span></div>
        <input type="range" min="${lo}" max="${hi}" step="${step}" value="${val}" data-msg="${msg}">`;
      target.appendChild(div);
      const inp = div.querySelector("input");
      const out = div.querySelector(".range-val");
      inp.oninput = ()=> out.textContent = inp.value+"°C";
      inp.onchange = async ()=> {
        try{ await api("/api/write", {method:"POST", body:JSON.stringify({circuit:"ctls2", msg, value:parseFloat(inp.value)})});
             toast(name+" → "+inp.value+"°C","ok");
        } catch(e){ toast("Error: "+e.message,"err"); }
      };
    });
  }
  render($("setp-sliders"), SETPOINT_DEFS);
  render($("flow-sliders"), FLOW_DEFS);
}

// Control mode — read-only gate
let CONTROL_ACTIVE = false;
async function loadControlStatus(){
  try{
    const s = await api("/api/control/status");
    CONTROL_ACTIVE = s.active;
    $("control-readonly").style.display = s.active ? "none" : "";
    $("control-active").style.display = s.active ? "" : "none";
    if(s.active){
      $("control-expires").textContent = new Date(s.expires_at*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
      $("control-last-ack").textContent = new Date(s.last_ack_at*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"});
    }
  } catch(e){ /* status polling failure isn't worth a toast */ }
}
async function enableControl(){
  if(!$("control-confirm-chk").checked) return toast("Marca la casilla de confirmación primero","err");
  try{
    await api("/api/control/enable", {method:"POST", body:JSON.stringify({confirm:true})});
    toast("Control total activado","ok");
    $("control-confirm-chk").checked = false;
    loadControlStatus();
  } catch(e){ toast("Error: "+e.message,"err"); }
}
async function ackControl(){
  try{ await api("/api/control/ack", {method:"POST"}); toast("Confirmado","ok"); loadControlStatus(); }
  catch(e){ toast("Error: "+e.message,"err"); }
}
async function disableControl(){
  try{ await api("/api/control/disable", {method:"POST"}); toast("Vuelto a solo lectura","ok"); loadControlStatus(); }
  catch(e){ toast("Error: "+e.message,"err"); }
}
setInterval(()=>{ if(document.querySelector(".tab.active").dataset.tab==="controls") loadControlStatus(); }, 15000);

// Optimizer
async function toggleOptimizer(){
  const enable = $("opt-toggle").checked;
  try{ await api("/api/optimizer", {method:"POST", body:JSON.stringify({enable})});
       toast("Optimizer "+(enable?"ON":"OFF"), "ok"); loadState();
  } catch(e){ toast("Error: "+e.message,"err"); }
}
async function loadDecisions(){
  try{
    const list = await api("/api/decisions?limit=80");
    const tb = $("dec-tbody"); tb.innerHTML="";
    if(!list.length){ tb.innerHTML='<tr><td colspan="3" class="empty">No decisions logged yet.</td></tr>'; return; }
    list.slice().reverse().forEach(d=>{
      const tr = document.createElement("tr");
      tr.innerHTML = `<td class="dec-time">${fmtTime(d.ts)}</td><td><span class="badge bg-a">${d.kind}</span></td><td>${d.reason}</td>`;
      tb.appendChild(tr);
    });
  } catch(e){ toast("Decisions load failed: "+e.message,"err"); }
}

// Charts — Chart.js with a guard against the "canvas in use" race
let CHARTS_BUSY = false;
async function loadSeries(series, hours){
  const data = await api(`/api/history?series=${series}&hours=${hours}`);
  return data.map(p=>({x: p.ts*1000, y: p.value}));
}
function destroyChart(canvasId){
  // Use Chart.js' own global registry so we catch any chart attached to this
  // canvas, even one we never tracked in our local CHARTS map.
  const existing = Chart.getChart(canvasId);
  if (existing) existing.destroy();
}
function mkChart(canvasId, datasets, yUnit){
  destroyChart(canvasId);
  const ctx = $(canvasId).getContext("2d");
  return new Chart(ctx, {
    type: "line",
    data: { datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: false, parsing: false,
      plugins: { legend: { labels: { color: "#94a3b8", font:{size:11} } } },
      scales: {
        x: { type:"time", time:{ unit:"hour", tooltipFormat:"PP HH:mm" },
             ticks:{color:"#94a3b8",font:{size:10}}, grid:{color:"rgba(148,163,184,.1)"} },
        y: { ticks:{color:"#94a3b8",font:{size:10}, callback:v => v+(yUnit||"")},
             grid:{color:"rgba(148,163,184,.1)"} }
      }
    }
  });
}
async function loadCharts(){
  if (CHARTS_BUSY) return;
  CHARTS_BUSY = true;
  const h = parseInt($("chart-window").value);
  try{
    const [room, setp, out, supply, ret, dt, cop, pin, pout] = await Promise.all([
      loadSeries("room_temp", h),
      loadSeries("setpoint_actual", h),
      loadSeries("outside_temp", h),
      loadSeries("supply_temp", h),
      loadSeries("return_temp", h),
      loadSeries("delta_t", h),
      loadSeries("cop", h),
      loadSeries("power_in", h),
      loadSeries("power_out", h),
    ]);
    mkChart("ch-temps", [
      { label:"Room",     data:room,   borderColor:"#38bdf8", backgroundColor:"transparent", tension:.3, pointRadius:0 },
      { label:"Setpoint", data:setp,   borderColor:"#fbbf24", backgroundColor:"transparent", tension:.3, pointRadius:0, borderDash:[4,4] },
      { label:"Outdoor",  data:out,    borderColor:"#a78bfa", backgroundColor:"transparent", tension:.3, pointRadius:0 },
      { label:"Supply",   data:supply, borderColor:"#fb923c", backgroundColor:"transparent", tension:.3, pointRadius:0 },
      { label:"Return",   data:ret,    borderColor:"#4ade80", backgroundColor:"transparent", tension:.3, pointRadius:0 },
    ], "°C");
    mkChart("ch-dt",
      [{ label:"ΔT", data:dt, borderColor:"#a78bfa", backgroundColor:"rgba(167,139,250,.15)", fill:true, tension:.3, pointRadius:0 }],
      " K");
    mkChart("ch-pow", [
      { label:"Electric", data:pin,  borderColor:"#fbbf24", backgroundColor:"transparent", tension:.3, pointRadius:0 },
      { label:"Thermal",  data:pout, borderColor:"#4ade80", backgroundColor:"transparent", tension:.3, pointRadius:0 },
    ], " kW");
    mkChart("ch-cop",
      [{ label:"COP", data:cop, borderColor:"#4ade80", backgroundColor:"rgba(74,222,128,.15)", fill:true, tension:.3, pointRadius:0 }]);
  } catch(e){ toast("Chart load failed: "+e.message, "err"); }
  finally { CHARTS_BUSY = false; }
}

// Diagnostics
function renderCompatCard(devices, compat){
  const el = $("compat-card");
  $("compat-dl").href = BASE+"/api/compat_report";
  if(!devices.length){
    el.innerHTML = `<h2 style="margin-top:0">Hardware compatibility</h2>`+
      `<div class="sub">No bus scan result yet — try "Re-scan bus" below, or wait for boot to finish.</div>`;
    return;
  }
  const badgeFor = id => compat.matched.some(d=>d.id===id) ? '<span class="badge bg-g">tested ✓</span>'
    : compat.mismatched_firmware.some(d=>d.id===id) ? '<span class="badge bg-y">different firmware</span>'
    : '<span class="badge bg-r">untested model</span>';
  const rows = devices.map(d =>
    `<div class="thermo-line">addr ${d.address} · ${d.manufacturer} <span style="color:var(--t)">${d.id}</span> SW${d.sw_version} HW${d.hw_version} ${badgeFor(d.id)}</div>`
  ).join("");
  const anyIssue = compat.mismatched_firmware.length || compat.unknown_device.length;
  el.innerHTML = `<h2 style="margin-top:0">Hardware compatibility</h2>${rows}` +
    `<div class="sub" style="margin-top:.5rem">${anyIssue
      ? '⚠ Tested only against Vaillant HMU00 SW0901/HW5103 + CTLS2 SW0509/HW1304 + VWZ SW0522/HW5103. Yours differs — it may still work, but if something looks wrong, hit "Report on GitHub" below to help us support it.'
      : '✓ Matches the exact hardware this add-on has been tested on.'}</div>`;
}
let LAST_COMPAT_REPORT = null;
async function loadCompat(){
  try{
    const r = await api("/api/compat_report");
    LAST_COMPAT_REPORT = r;
    renderCompatCard(r.detected_devices, r.compatibility);
  } catch(e){ /* non-fatal — diagnostics still works without it */ }
}
async function rescanBus(){
  try{ await api("/api/ebusd_scan", {method:"POST"});
       toast("Full bus re-scan requested — this can take a minute","info");
       setTimeout(loadDiag, 20000);
  } catch(e){ toast("Error: "+e.message,"err"); }
}
function compatTitle(r){
  const c = r.compatibility;
  if (c.unknown_device.length) return "Device support: "+c.unknown_device.map(d=>d.id).join(", ");
  if (c.mismatched_firmware.length) return "Firmware mismatch: "+c.mismatched_firmware.map(d=>d.id+" SW"+d.sw_version+"/HW"+d.hw_version).join(", ");
  return "Compatibility report";
}
async function reportToGithub(){
  try{
    const r = LAST_COMPAT_REPORT || await api("/api/compat_report");
    const base = "https://github.com/onemanfoundry/ha-genia-air/issues/new";
    const params = new URLSearchParams({ template: "device-support.yml", title: compatTitle(r) });
    const withReport = new URLSearchParams(params);
    withReport.set("compat-report", JSON.stringify(r));
    // GitHub/browsers get unreliable well before any official URL-length
    // limit — stay under a safe margin, else open without the pre-filled
    // field and ask the user to attach the downloaded JSON instead.
    const full = base+"?"+withReport.toString();
    const usedFull = full.length < 7500;
    window.open(usedFull ? full : base+"?"+params.toString(), "_blank");
    toast(usedFull ? "Opening a pre-filled GitHub issue…"
                    : "Report is large — attach the downloaded JSON on the issue page", "info");
  } catch(e){ toast("Error: "+e.message,"err"); }
}
async function loadDiag(){
  loadCompat();
  try{
    const [msgs, h] = await Promise.all([api("/api/messages"), api("/api/health")]);
    // ebusd status card
    const ec = $("ebusd-card");
    const running = h.ebusd_running;
    ec.innerHTML =
      `<div style="font-size:2rem">${running?"🟢":"🔴"}</div>`+
      `<div style="flex:1;min-width:200px">`+
        `<div style="font-weight:600">ebusd ${running?"running":"stopped"}</div>`+
        `<div class="sub">PID ${h.ebusd_pid ?? "—"} · restarts: ${h.ebusd_restarts} · MQTT ${h.mqtt_connected?"✓":"✗"} · messages: ${h.state_size} · uptime ${fmtAge(h.uptime_seconds)}</div>`+
      `</div>`;
    // messages table
    const tb = $("diag-tbody"); tb.innerHTML="";
    if(!msgs.length){ tb.innerHTML='<tr><td colspan="4" class="empty">No ebusd messages yet.</td></tr>'; return; }
    msgs.forEach(m=>{
      const ageClass = m.age_seconds<120?"diag-fresh":m.age_seconds<600?"diag-old":"diag-stale";
      const fields = Object.entries(m.fields).map(([k,v])=>`<span class="badge bg-m">${k}=${v}</span>`).join(" ");
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${m.circuit}</td><td>${m.msg}</td><td>${fields}</td><td class="${ageClass}">${fmtAge(m.age_seconds)}</td>`;
      tb.appendChild(tr);
    });
  } catch(e){ toast("Diagnostics load failed: "+e.message,"err"); }
}
async function forceRead(){
  try{ await api("/api/force_read", {method:"POST"}); toast("Force-read dispatched","info");
       setTimeout(loadDiag, 6000);
  } catch(e){ toast("Error: "+e.message,"err"); }
}
async function ebusdAction(action){
  try{ const r = await api("/api/ebusd", {method:"POST", body:JSON.stringify({action})});
       toast("ebusd "+action+(r.running?" → running":" → stopped"),"ok");
       setTimeout(loadDiag, 4000);
  } catch(e){ toast("Error: "+e.message,"err"); }
}

// Boot
loadState();
setInterval(loadState, 5000);
setInterval(()=>{ if(document.querySelector(".tab.active").dataset.tab==="optimizer") loadDecisions(); }, 8000);
</script>
</body>
</html>
"""


# ───────────────────────────────────────────────────────────────────────────
# Boot
# ───────────────────────────────────────────────────────────────────────────

def boot() -> None:
    db_init_safe()
    _install_signal_handlers()
    ebusd_start()
    mqtt_start()

    # Initial sync runs later than before — give ebusd a few seconds to
    # finish its bus scan and seed the MQTT discovery before we ask for reads.
    SCHEDULER.add_job(task_initial_sync, "date",
                      run_date=datetime.utcnow() + timedelta(seconds=20))
    SCHEDULER.add_job(mqtt_post_connect_subs, "date",
                      run_date=datetime.utcnow() + timedelta(seconds=8))
    SCHEDULER.add_job(control_recover_on_boot, "date",
                      run_date=datetime.utcnow() + timedelta(seconds=9))
    SCHEDULER.add_job(task_initial_sync, "interval", minutes=20,
                      id="initial_sync_periodic")
    SCHEDULER.add_job(task_snapshot_history, "interval", minutes=1,
                      id="snapshot_history", max_instances=1)
    SCHEDULER.add_job(task_optimize, "interval",
                      minutes=CONF["optimize_cycle_min"], id="optimize")
    SCHEDULER.add_job(task_health_check, "interval", minutes=2,
                      id="health_check")
    SCHEDULER.add_job(task_publish_states, "interval", seconds=20,
                      id="publish_states")
    SCHEDULER.add_job(ebusd_watchdog, "interval", seconds=15,
                      id="ebusd_watchdog")
    SCHEDULER.add_job(task_control_watchdog, "interval", seconds=30,
                      id="control_watchdog")
    SCHEDULER.start()
    log.info("Scheduler started — ebusd PID=%s, initial sync in 20s",
             EBUSD_PROCESS.pid if EBUSD_PROCESS else "?")


if __name__ == "__main__":
    boot()
    # threaded=True: without it, a slow synchronous request (e.g. /api/compat_report's
    # raw ebusd TCP scan, up to ~8s) blocks every other request on the same
    # single-threaded dev server — including /api/control/status polling, which
    # matters now that the Controls tab depends on that polling for the
    # session countdown/ack UI to stay responsive.
    app.run(host="0.0.0.0", port=8099, threaded=True)
