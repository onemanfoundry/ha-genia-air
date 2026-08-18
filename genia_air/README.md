# Vaillant Genia Air

Standalone Home Assistant add-on to **control and optimize** a Vaillant
Genia Air heat pump.

This add-on is **fully self-contained**: it bundles the
[`ebusd`](https://github.com/john30/ebusd) daemon, the Vaillant message
definitions, its own UI, its own history database and its own optimizer.
The only external dependency is the MQTT integration (Mosquitto or
equivalent) that Home Assistant already needs.

## Screenshots

> Screenshots will be added shortly. Tabs are:
> Overview · Charts · Controls · Optimizer · Diagnostics.

## What you get

- **Live overview** with KPI cards: ΔT, COP, modulation, electric/thermal
  power, flow temps, runtime hours.
- **Thermostat** for zone 1 with mode switching (off/heat/cool/auto) and
  inline setpoint editing.
- **Charts** (6 h / 24 h / 72 h / 7 days): temperatures, ΔT, electric vs
  thermal power, instantaneous COP. Stored locally in SQLite, snapshotted
  every minute.
- **Controls** for every zone setpoint, the heating curve and safety
  limits.
- **Optimizer** — deterministic control loop that:
  - Computes a weather-compensated max-flow-temp target and writes it to
    the heat pump every cycle when the outdoor reading changes.
  - Clamps any unsafe user-set max/min flow temp back into the safe range
    (anti-condensation and underfloor heating ceilings).
  - Performs a seasonal heat ↔ cool switchover based on the rolling
    outdoor average, with anti-flap hysteresis.
  - Alerts when ΔT(supply − return) drifts from target by > 0.8 K.
- **Diagnostics** tab listing every ebusd message seen, their ages and a
  force-read trigger.
- **MQTT Discovery** publishes a minimal `Genia Air (addon)` device into
  Home Assistant with 5 entities for automations.

## Installation

You need an eBUS adapter wired to your heat pump and reachable over the
network or USB. Most users have a network-attached
[eBUS Adapter Shield 3](https://adapter.ebusd.eu/) or an ESP-based
adapter that exposes the bus on a TCP port (default `9999`).

1. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋯ → Repositories**
   and add `https://github.com/onemanfoundry/ha-genia-air`.
2. Install **Vaillant Genia Air**.
3. Set `ebus_device` in the configuration tab to **your** adapter (the
   `192.168.1.100` default is only a placeholder — replace it with your
   adapter's real address), e.g.:
   - `ens:<adapter-ip>:9999` for a network adapter (ens = enhanced),
   - `enh:<adapter-ip>:9999` for an old-style network adapter,
   - `/dev/ttyUSB0` for a USB-attached adapter.
4. Start the add-on. Click **Open Web UI**, or use the **Genia Air**
   entry that appears in the Home Assistant sidebar.

## Configuration

| Option | Default | Notes |
|---|---|---|
| `ebus_device` | `ens:192.168.1.100:9999` | eBUS adapter URL (`ens:`, `enh:`, `/dev/tty*`) |
| `topic_prefix` | `ebusd` | MQTT topic prefix used by ebusd |
| `zone_count` | `1` | v0.2 controls zone 1 only |
| `optimize_flow_temp` | `true` | Master switch for the optimizer |
| `target_delta_t` | `5.0` K | Heating-side ΔT target used for anomaly alerts |
| `min_flow_temp_safe` | `14.0` °C | Anti-condensation floor in cooling mode |
| `max_flow_temp_safe` | `35.0` °C | Underfloor heating ceiling |
| `summer_temp_limit` | `19.0` °C | Heat ↔ cool seasonal switchover pivot |
| `optimize_cycle_minutes` | `5` | How often the optimizer evaluates |
| `ebusd_log_level` | `notice` | `error`, `notice`, `info`, `debug` |

## Compatibility

This add-on has been **tested against exactly one unit**, identified as
follows on the eBUS (address · manufacturer · product ID · firmware):

| Address | Device | Manufacturer | ID | SW version | HW version |
|---|---|---|---|---|---|
| `08` | Heat Management Unit | Vaillant | `HMU00` | `0901` | `5103` |
| `15` | Sigma 2 room controller | Vaillant | `CTLS2` | `0509` | `1304` |
| `76` | Compressor module | Vaillant | `VWZ`   | `0522` | `5103` |

Other Genia Air / aroTHERM units, other firmware revisions, or other Vaillant
controllers **may work** — the eBUS schema is shared across much of the
range — but nothing outside the table above has been verified.

**The Diagnostics tab tells you where you stand.** It scans the bus via
ebusd and shows each detected device against the table above:
tested ✓ (exact match), different firmware (same product, unverified
version), or untested model (a product ID we've never seen). If you're not
an exact match, things may well still work — the add-on won't stop you —
but if entities look wrong or messages fail to parse, that's the first
thing to check.

**Help us support your unit.** Click **🐙 Report on GitHub** in Diagnostics —
it opens a pre-filled *device support* issue on GitHub with the
compatibility report already pasted into the form; you just need to add a
line about what's actually going wrong and hit submit. (If the report is
too large for a URL, the button opens the issue anyway and you attach the
**⬇ Download JSON** file instead.) The report is built to be safe to post
publicly: it contains only eBUS protocol identifiers (bus address,
manufacturer, product/firmware IDs) and message/field **names** — never
live sensor readings, your network address, or any credentials. No account
or personal information is collected beyond whatever you choose to put in
the GitHub issue itself, and beyond signing in to GitHub to submit it.

## Troubleshooting

**Charts are empty.** The add-on snapshots one row per metric every
minute into a local SQLite database. Charts will be empty for the first
few minutes after install; come back in 10–20 min.

**Most entities show "unavailable" right after install.** eBUS messages
on the CTLS2 controller side are only emitted by the heat pump on
demand. The add-on triggers a forced read at boot and every 20 min
afterwards, plus you can press **Force-read all** in the Diagnostics tab
to trigger an extra round.

**MQTT connect fails (rc=5 in the log).** Means *not authorized*. The
add-on asks the Supervisor for the broker credentials at boot. Make
sure the MQTT integration is installed and a broker is running (the
Mosquitto add-on is the easiest option).

**Setpoint slider in Controls keeps jumping back.** Charts and sliders
re-fetch state every five seconds. Click and *release* the slider; the
write goes out on `change`, not `input`.

## Architecture in one paragraph

A single Python file (`/usr/bin/genia_air.py`) runs Flask for the UI,
paho-mqtt for the eBUS data, APScheduler for the optimizer cycles and
SQLite for history. The embedded HTML panel (one big string) renders
with Chart.js. The add-on listens to `<topic_prefix>/+/+`, decodes the
ebusd JSON payloads, computes derived values (ΔT, COP, hvac action),
runs the optimizer every five minutes and publishes a tiny MQTT Discovery
device back into Home Assistant.

See [`ARCHITECTURE.md`](https://github.com/onemanfoundry/ha-genia-air/blob/main/ARCHITECTURE.md)
in the repository for the full design notes.

## What's intentionally NOT here yet

- ML-based optimization (the optimizer is deterministic; ML is planned).
- Multi-zone (Z2/Z3) support.
- Domestic hot-water control (the Genia Air doesn't drive DHW in many
  installations).
- Direct pump-speed actuation (the HMU manages pump speed internally
  and does not expose a PWM control over eBUS).

## License

Apache-2.0.
