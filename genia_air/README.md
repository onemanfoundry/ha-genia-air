# Vaillant Genia Air

> **This is a standalone Home Assistant Add-on (a Docker container with its
> own UI, reachable via ingress), not a HACS custom integration.** There is
> no `custom_components/` entry, no `config_flow`, no native `climate`
> entity to add — you install it from Settings → Add-ons like any other
> add-on, and it talks back to Home Assistant only via MQTT Discovery (a
> handful of read-only entities for automations). If you were expecting a
> HACS integration, this isn't that — see [Architecture](#architecture-in-one-paragraph)
> for why.

> **⚠️ Status: Beta.** Read-only by default — control is opt-in and
> automatically expires. See [Safety model](#safety-model) before enabling
> control on a real heat pump. A live round-trip against real hardware
> (write → confirm the boiler applied it → let the session expire →
> confirm the original value comes back) is still pending; treat this as
> beta until that's confirmed and it's run incident-free across more than
> one installation.

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

## Quick Start (5 minutes)

You need an eBUS adapter wired to your heat pump and reachable over the
network or USB. Most users have a network-attached
[eBUS Adapter Shield 3](https://adapter.ebusd.eu/) or an ESP-based
adapter that exposes the bus on a TCP port (default `9999`).

1. Install the **Mosquitto broker** add-on (Settings → Add-ons → Add-on
   Store) if you don't already have an MQTT broker — this add-on needs one.
2. Connect/power your eBUS adapter and note its IP (or USB device path).
3. **Settings → Add-ons → Add-on Store → ⋯ → Repositories**, add
   `https://github.com/onemanfoundry/ha-genia-air`.
4. Install **Vaillant Genia Air**.
5. Set `ebus_device` in the configuration tab to **your** adapter (the
   `192.168.1.100` default is only a placeholder), e.g.:
   - `ens:<adapter-ip>:9999` for a network adapter (ens = enhanced),
   - `enh:<adapter-ip>:9999` for an old-style network adapter,
   - `/dev/ttyUSB0` for a USB-attached adapter.
6. Start the add-on.
7. Click **Open Web UI** (or the **Genia Air** sidebar entry).
8. Open the **Diagnostics** tab — confirm your heat pump was detected and
   check the compatibility banner.
9. **Leave it read-only for now.** Don't touch the Controls tab yet.
10. Watch the **Overview** tab for a few minutes and confirm telemetry
    (room temp, flow/return temps, ΔT, COP) looks plausible for your
    system. If numbers look wrong, fix that before going anywhere near
    full control — see [Before enabling control](#before-enabling-control)
    below.

### Before enabling control

Once telemetry looks right, go through this before ticking "I understand"
on the Controls tab for the first time:

- [ ] Diagnostics shows your correct heat pump/controller detected (not an
      "unknown device" or a firmware mismatch you don't understand).
- [ ] Flow temperature looks plausible for your system (not `0`, not a
      wildly implausible number).
- [ ] Return temperature looks plausible, and flow > return while heating.
- [ ] Outdoor temperature roughly matches reality.
- [ ] COP looks plausible (typically 2–5 for an air-source heat pump —
      not negative, not in the hundreds).
- [ ] ΔT (flow − return) looks plausible for your emitters (typically a
      few K, not near-zero or wildly large while actively heating).
- [ ] You have a way to control the heat pump manually if something looks
      wrong (the boiler's own panel, or another known-good path) — don't
      make this addon your only way to touch the system on day one.
- [ ] You've read [Safety model](#safety-model) below and understand that
      control sessions expire and revert on their own.

### Try it without hardware

Don't have an eBUS adapter yet, or just want to see how this works before
wiring anything up? Set `simulate_hardware: true` in the configuration tab
and start the addon — no adapter, no MQTT broker needed. You get the full
UI (all 5 tabs) running against synthetic-but-plausible telemetry that
drifts slowly over time, and you can safely enable full control, change
setpoints, and watch a session expire and revert — nothing here ever
touches a real device, because there isn't one. This is also exactly what
the automated test suite uses to exercise the full enable → write →
expire → restore cycle without needing real hardware in CI
(`test_simulate_mode_full_control_round_trip` in
`tests/test_control_mode.py`). Turn `simulate_hardware` back off and
configure `ebus_device` when you're ready to connect a real unit.

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
| `control_session_minutes` | `60` | How long a full-control session lasts before auto-expiring — see [Safety model](#safety-model) |
| `control_ack_grace_minutes` | `15` | How long a full-control session can go without a "still looks right" confirmation before auto-reverting |
| `control_notify_target` | *(empty → `notify.notify`)* | HA notify service to call when a session auto-reverts, e.g. `notify.mobile_app_myphone` |
| `stale_data_minutes` | `20` | If nothing in the bus telemetry has updated within this window, health reports it and an active control session reverts — a frozen sensor is still "present" but no longer true |
| `simulate_hardware` | `false` | Run against synthetic telemetry instead of a real eBUS adapter — see [Try it without hardware](#try-it-without-hardware) |

## Safety model

**Read-only by default.** Nothing — not a manual setpoint change, not the
HVAC mode buttons, not the autonomous optimizer — writes to the boiler until
you explicitly turn on **full control** from the Controls tab (you have to
tick "I understand this will control my real heating system" first; there's
no bare on-switch).

**Sessions are bounded and self-healing, not "on forever":**
- A session auto-expires after `control_session_minutes` regardless of
  anything else.
- While active, the app periodically needs you to confirm the current values
  still make sense ("✅ Los valores tienen sentido" in the Controls tab). Go
  quiet for longer than `control_ack_grace_minutes` and it reverts on its
  own — this is the actual safety net, not the hard expiry, since it catches
  "I left and forgot about this" faster than a 60-minute timer would.
- **Reverting is a real restore, not just "stop writing."** The moment you
  enable full control, the add-on snapshots every writable value (zone
  setpoints, HVAC mode, flow-temp limits) as they stood *before* the
  session. On expiry, a stale ack, or clicking "🔒 Volver a solo lectura"
  yourself, those exact values get re-published — your boiler ends up back
  where it started, not wherever the last write happened to leave it.
- Every auto-revert calls a Home Assistant `notify.*` service (configurable
  via `control_notify_target`) so you get a push/notification explaining
  what happened and why.

This applies uniformly — a manual slider drag and the optimizer's automatic
weather-compensated writes share the same session and the same gate. There's
no separate "let the optimizer run forever without asking" mode; if you want
the optimizer active for days, you re-confirm every `control_ack_grace_minutes`
like anything else.

**Fail-closed, not fail-open.** If ebusd dies, MQTT disconnects, or
telemetry simply stops updating (a frozen sensor is still "present" in the
UI, it's just no longer true) while a session is active, the session ends
immediately (checked every 30s) rather than waiting for its own
expiry/ack timers — the addon can't trust what it's reading, so it can't
safely keep controlling anything. Separately, every write (manual,
optimizer, or a safety clamp) goes through one validated choke point that
refuses non-finite values (NaN/Infinity) and refuses to publish at all
when MQTT isn't actually connected — the UI gets a clear error instead of
a silent no-op.

**Validated so far:** the full gating/snapshot/expiry/revert/fail-closed
logic has 55 unit tests (`tests/test_control_mode.py` + `test_logic.py`),
including a full enable → write → expire → restore round-trip run against
the built-in [simulator](#try-it-without-hardware)
(`test_simulate_mode_full_control_round_trip`). The addon has also been
built and started end-to-end on a real HAOS Supervisor (see
`INFRA/servers.md` VM 120). What's still missing is the same round-trip
against **real** hardware (enable control → write a real setpoint → let it
auto-revert → confirm the boiler actually reports the original value
again) — the eBUS Adapter Shield used for that test needs a power-cycle
first (its TCP port was refusing new connections, unrelated to this
addon). The simulated round-trip proves the *logic* is correct; it can't
prove the real MQTT↔ebusd↔eBUS↔heat-pump chain behaves the same way.
**This addon has not yet been validated against real hardware end-to-end
and should be treated as beta** until that round-trip (and ideally a few
weeks of incident-free use across more than one installation) is
confirmed.

## Compatibility

This add-on has been **tested against exactly one unit**, identified as
follows on the eBUS (address · manufacturer · product ID · firmware):

| Address | Device | Manufacturer | ID | SW version | HW version |
|---|---|---|---|---|---|
| `08` | Heat Management Unit | Vaillant | `HMU00` | `0901` | `5103` |
| `15` | Sigma 2 room controller | Vaillant | `CTLS2` | `0509` | `1304` |
| `76` | Compressor module | Vaillant | `VWZ`   | `0522` | `5103` |

**Read / write / optimizer support for that exact unit:**

| Read (telemetry) | Write (setpoints/mode) | Optimizer | Round-trip verified against real hardware | State |
|---|---|---|---|---|
| ✅ | ✅ (via full-control sessions) | ✅ | ⏳ pending (see [Safety model](#safety-model)) | 🟡 Beta — single unit |

We don't have a second real unit to test against, so there's no matrix of
"other models" here yet — inventing plausible-sounding rows for untested
hardware would be worse than an honest single-row table. Other Genia Air /
aroTHERM units, other firmware revisions, or other Vaillant controllers
**may work** (the eBUS schema is shared across much of the range) but
nothing beyond the row above has actually been verified. **This grows from
real user reports, not guesses** — see "Help us support your unit" below;
each confirmed report becomes a new row.

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

**Note on `_reference/custom_components_legacy/`:** the repo also carries an
old, superseded `custom_components`-style prototype from before this
project settled on the add-on architecture. It's kept only as design
history (referenced by `ARCHITECTURE.md`'s "write path" notes) — it is
**not installable, not maintained, and not what you get when you install
this add-on**. Ignore it unless you're specifically digging into the
project's history.

## What's intentionally NOT here yet

- ML-based optimization (the optimizer is deterministic; ML is planned).
- Multi-zone (Z2/Z3) support.
- Domestic hot-water control (the Genia Air doesn't drive DHW in many
  installations).
- Direct pump-speed actuation (the HMU manages pump speed internally
  and does not expose a PWM control over eBUS).

## License

Apache-2.0.
