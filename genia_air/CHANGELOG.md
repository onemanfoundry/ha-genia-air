# Changelog

## 0.8.0 — 2026-10-14

- **Writes are confirmed, not assumed.** Publishing a write to MQTT only
  hands it to ebusd; whether the controller accepted it shows up on
  ebusd's log (`[mqtt notice] write …` or `[mqtt error] write …: ERR …`).
  The add-on now waits up to 8 s for that verdict:
  - a setpoint / mode / slider change that fails on the bus returns an
    error and the UI shows it, instead of a green "→ 21.5°C" toast;
  - an unconfirmed write is reported as "sent, not confirmed by the bus";
  - **reverting a control session retries failed writes once and reports
    what really happened** ("restored 8 of 10 value(s). Could NOT restore:
    z1ManualTemp — check them on the heat pump's own controller"), in the
    decision log and in the Home Assistant notification. It used to say
    "restored 10 value(s)" whatever the bus answered.
- Found while attempting the real-hardware round-trip on 2026-10-14: over
  a lossy WiFi adapter link both the write and the restore failed on the
  bus while the UI reported success. The heat pump never changed, but the
  user could not have known.

## 0.7.5 — 2026-10-14

- **Charts show gaps instead of inventing lines.** When a series has no
  data for more than 5 minutes (compressor off, adapter unreachable), the
  chart breaks the line; it used to draw a straight diagonal across days.
- **Optimizer decision log survives restarts.** It was served from memory
  only, so the tab said "No decisions logged yet" after every add-on start
  although every decision is stored in the database. It now reads the
  database (falling back to memory if that fails).
- Two optimizer decisions in the same second no longer lose the second
  one: the `decisions` table keyed rows by the second, and the failed
  insert was swallowed. It now has its own id (existing rows are migrated
  on start) and a failed write is logged.
- Changing the chart window (6 h … 7 days) reloads the charts; it used to
  need a click on Refresh.
- Diagnostics shows replayed (`retained`) values in grey rather than red:
  for settings that rarely change, a replayed value is normal, not an error.

## 0.7.4 — 2026-10-14

- **Compatibility table fixed for address 0x76.** It listed `VWZ`
  SW0522/HW5103, copied from the header comment of the upstream
  `76.vwz.csv` (another contributor's unit). The tested unit's bus scan
  reports `VWZIO` SW0202/HW0103, so Diagnostics flagged the reference
  hardware itself as an "untested model". Table, banner text and READMEs
  now carry the scanned identity.

## 0.7.3 — 2026-10-14

- **Stale values no longer look live.** ebusd publishes with
  `--mqttretain`, so on every add-on start the broker replayed the last
  value of every message and the add-on stamped it as just received. A
  power reading from days earlier (4.4 kW electric with the compressor at
  0 %) showed as current, went into the charts and reset the frozen-telemetry
  check. Retained replays are now kept with an unknown age, never overwrite a
  live value, and show as `retained` in Diagnostics.
- Live telemetry (supply/return, electric/thermal power, modulation, flow)
  now reads as unknown once it has not refreshed for `stale_data_minutes`,
  instead of repeating the last value — e.g. when active reads keep failing
  on a bad adapter link. Charts get a gap instead of a flat line that never
  happened. Settings (setpoints, curve, limits) are unaffected.

## 0.7.2 — 2026-10-14

- New option `ebusd_latency_ms` (default `0`, unchanged behaviour), passed
  to ebusd as `--latency`. On a network adapter with a slow or lossy link
  (an ESP32 adapter on congested 2.4 GHz WiFi measured 95–300 ms ping),
  slave answers arrive after ebusd's 25 ms receive timeout, so every active
  read and the bus scan fail with `read timeout` / `SYN received`, and the
  Diagnostics compatibility check never gets a scan result. On that link,
  `50` took active reads from 0/10 to 3/10 and let the scan identify the
  HMU. It mitigates; the real fix is a better link (wired, or a stronger
  WiFi signal).

## 0.7.1 — 2026-10-14

- **UI and notifications are now English-only.** The Controls tab
  read-only/full-control gate, its toasts, the revert reasons logged to the
  decision history and the Home Assistant notifications sent on auto-revert
  were still in Spanish. Button labels changed accordingly:
  "✅ Values look right" and "🔒 Back to read-only".
- `tests/validate_repo.py` now fails CI if Spanish-only characters creep
  back into `genia_air.py`.

## 0.7.0 — 2026-08-24

- **Simulate mode** (`simulate_hardware: true`): run the entire addon —
  full UI, optimizer, and a real full-control enable → write → expire →
  restore cycle — against synthetic telemetry, with no eBUS adapter and no
  MQTT broker required. A fake `EBUSD_PROCESS`/`MQTT_CLIENT` pair swaps in
  underneath the existing (unmodified) `mqtt_publish_write()` choke point:
  every write path is exercised exactly as it would be against real
  hardware, just with a synthetic bus on the other end. Lets a new user
  try the addon before owning an adapter, and gives the test suite a real
  round-trip to run in CI.
- New option: `simulate_hardware` (default `false` — off unless you turn
  it on).
- New tests (55 total): the simulated MQTT client's write-back into
  `STATE`, `simulate_start()` producing a healthy boot state, and
  `test_simulate_mode_full_control_round_trip` — enable control, write a
  setpoint through the real API, confirm the simulated bus reflects it,
  let the session expire, confirm the original value comes back for real.
  This is the closest thing to the outstanding physical round-trip test
  until real hardware is available again (see README → Safety model for
  what it does and doesn't prove).

## 0.6.1 — 2026-08-24

- **Fail-closed on stale telemetry, not just a dead link.** A sensor that
  stopped updating is still "present" in `STATE` — it just isn't true
  anymore. New `stale_data_minutes` option (default 20): if nothing in
  `STATE` has updated within that window, health reports it and an active
  control session reverts immediately, same as a dead ebusd/MQTT link.
- Documentation pass following an external review: explicit "this is an
  Add-on, not a HACS integration" banner, a Beta status warning up top,
  a Quick Start (5-minute install) checklist, a "before enabling control"
  pre-flight checklist, and a read/write/optimizer support matrix for the
  one verified unit (no fabricated rows for untested hardware).
- `device-support` issue template now also asks for the eBUS adapter model
  and HA/Supervisor version.

## 0.6.0 — 2026-08-24

Hardening pass on the v0.5.0 safety model, following an external code
review focused on failure-mode behavior (what happens when things go
wrong, not just the happy path).

- **Fail-closed, not fail-open.** If ebusd dies or MQTT disconnects while
  a full-control session is active, the session ends immediately (checked
  every 30s) — restoring what it can and flipping to read-only — instead
  of waiting for the session's own expiry/ack timers. An unhealthy link
  means the addon can't trust its own reads or reliably write anyway.
- **`mqtt_publish_write()` is now the single validated choke point** every
  write path goes through (manual API, optimizer, safety clamps, session
  restore): refuses non-finite values (NaN/Infinity, reachable via a JSON
  payload since Python's `json` module accepts them as an extension) and
  refuses to publish when MQTT isn't actually connected — a stale client
  reference used to be treated as "connected enough to try." Callers now
  check the return value instead of assuming success.
- `/api/write`, `/api/mode`, `/api/setpoint` return `503` (and log a
  `write_rejected` decision) instead of silently reporting `{"ok": true}`
  when the underlying publish didn't actually happen.
- **Audit log now records old value → new value and a source** (`manual`,
  `optimizer`, `safety`) on every write decision, not just the new value —
  answers "what was it before, and who/what changed it."

## 0.5.0 — 2026-08-24

- **Read-only by default — full control is now a gated, time-boxed
  session.** No write (manual setpoint/mode changes, nor the autonomous
  optimizer) reaches the boiler until you explicitly enable full control
  from the Controls tab. Sessions auto-expire (`control_session_minutes`,
  default 60 min) and require periodic "still looks right" confirmation
  (`control_ack_grace_minutes`, default 15 min) or they auto-revert —
  restoring the exact pre-session values, not just stopping writes — and
  send an HA notification explaining why. See README → *Safety model*.
  **This is a behavior change**: the optimizer and manual controls no
  longer act until you turn full control on at least once after updating.
- New options: `control_session_minutes`, `control_ack_grace_minutes`,
  `control_notify_target`.
- New endpoints: `GET /api/control/status`, `POST /api/control/enable`,
  `POST /api/control/ack`, `POST /api/control/disable`. `/api/write`,
  `/api/mode`, `/api/setpoint` now return `403` outside an active session.
- The pre-session snapshot now persists to `/data/control_snapshot.json`
  and is restored on boot if the addon restarts mid-session (crash, update)
  — a full-control session no longer silently loses its restore baseline
  if the addon goes down while active.
- Fixed: the ΔT anomaly alert (pure monitoring, no actuation) was
  accidentally gated behind the same read-only check as actual writes —
  it now always runs regardless of control-mode state.
- Fixed: a rare race where the watchdog could revert a session that had
  just been renewed at the expiry boundary.
- `app.run(..., threaded=True)` — the Diagnostics tab's ebusd scan could
  block the whole UI, including control-session polling, for several
  seconds on a single-threaded dev server.
- Verified: 16 new unit tests, full regression suite green (38/38), and a
  real build+install+start against a live HAOS 18.2 Supervisor (VM 120,
  see `INFRA/servers.md`). Live round-trip against real eBUS hardware is
  pending a power-cycle of the test adapter — see README → *Safety model*.

## 0.4.0 — 2026-08-18

- **Hardware compatibility check.** Diagnostics now shows every device
  ebusd sees on the bus against the exact unit this add-on is tested on
  (Vaillant `HMU00` SW=0901/HW=5103, `CTLS2` SW=0509/HW=1304, `VWZ`
  SW=0522/HW=5103), flagging firmware mismatches and untested product IDs
  instead of failing silently.
- **Anonymous compatibility report.** New **⬇ Download JSON** button saves
  a report with device identification and message/field *names* only —
  never live readings, network addresses or credentials.
- **One-click GitHub report.** New **🐙 Report on GitHub** button opens a
  pre-filled `device-support` issue with the compatibility report already
  in the form (falls back to "attach the file" for reports too large for a
  URL) — no copy-paste, no account/write access needed from the add-on
  itself.
- New **"🔍 Re-scan bus"** action triggers a fresh `scan full` on ebusd on
  demand.
- New endpoints: `GET /api/compat_report`, `POST /api/ebusd_scan`.

## 0.3.0 — 2026-06-22

First community release. Repository polish + add-on store hygiene:

- **Genericized all defaults** — removed personal network/IP values; the
  `ebus_device` default is now an obvious placeholder you must replace.
- **Passes the official add-on linter**: dropped `boot`/`ingress_port`
  (they were set to their defaults) and removed the **deprecated 32-bit
  arches** (`armv7`/`armhf`/`i386`) per Home Assistant 2025.12. Supported
  arches are now `aarch64` and `amd64`.
- Added add-on **icon and logo**, a community-facing README (badges,
  one-click "add repository", features, screenshots placeholder) and a CI
  badge.
- Removed internal development notes from the published repo.

No functional changes to the control/optimizer logic vs 0.2.8.

## 0.2.8 — 2026-06-21

- **Clearer target temperature.** The thermostat card showed
  `Current setpoint: 0.0°C` because it always rendered the *heating*
  desired value, which the pump reports as 0 while cooling in summer.
  New `setpoint_effective` picks the cooling or heating setpoint based on
  what the unit is actually doing, labelled `heating`/`cooling` so it's
  unambiguous.
- **No more "UNKNOWN" activity pill.** `hvac_action` now falls back to
  inferring activity from the compressor (power/modulation, with the
  supply−return sign disambiguating heat vs cool in AUTO) when the raw
  hmu/State code isn't mapped. If it still can't tell, the pill is hidden
  instead of showing a confusing "UNKNOWN".

## 0.2.7 — 2026-06-19

- **Fix HTTP 403 on every write** (setpoint, mode, optimizer, manual
  write, ebusd control). The ingress guard required an `X-Hass-User`
  header that **Home Assistant Ingress never sends** — Ingress stamps
  `X-Ingress-Path` plus `X-Remote-User-Id` / `X-Remote-User-Name`. GET
  routes worked; any POST 403'd. The guard now trusts Ingress presence
  (`X-Ingress-Path`) as proof of an authenticated HA user, and the audit
  log records identity from `X-Remote-User-*`.

## 0.2.6 — 2026-06-19

- **Fix build on 32-bit ARM (armv7/armhf).** The Dockerfile asked for a
  non-existent `ebusd-26.1_armhf-bookworm_mqtt1.deb` (HTTP 404), so the
  add-on build failed on Raspberry Pi 32-bit installs. Upstream ships
  `armv7`, not `armhf` — both HA arches now map to the `armv7` `.deb`.
  amd64 / aarch64 / i386 were already correct.
- **USB adapter support.** `uart: true` in the add-on config so a
  USB-attached eBUS adapter (`/dev/ttyUSB0`) is actually mapped into the
  container — the README already documented it but the option was
  missing.
- Fix the `snapshots` table DDL (it declared two PRIMARY KEYs, so the
  composite-PK schema always errored and silently fell back to a
  no-dedup table). Now uses a real `PRIMARY KEY (ts, series)`.

## 0.2.5 — 2026-06-16

- Fix **Total hours / Heating hours / Cooling hours** stuck at "—". The
  bundled ebusd v26 CSV publishes these as `{"energy": <N>}`; the
  legacy LukasGrebe CSV used positional `"0"`. Try the named field
  first and fall back to `"0"` so both work.
- Better COP UX: when the compressor is idle (instantaneous COP would
  be null because electric power ≈ 0), fall back to a **30-minute
  rolling COP** computed from the SQLite history. If that's also
  empty, show `"idle"` instead of `—`.

## 0.2.4 — 2026-06-16

- Auto-fallback when the configured `ebus_device` TCP endpoint is
  unreachable: probe the host once at boot, and if it fails, scan the
  same `/24` for a host accepting connections on the same port. Helps
  when the persisted option points to an adapter that has moved or
  changed IP (in Sergio's case, .171 → .61). Concurrent probes via a
  32-worker thread pool, ~5 s scan time per /24.

## 0.2.3 — 2026-06-16

- Stop overriding the HA base image's s6-overlay init (drop the tini
  ENTRYPOINT). The Debian base ships with `/init` from s6-overlay which
  populates `/run/s6/container_environment/`; with-contenv reads from
  there. Removing s6 broke the env-vars path Python relied on.
- Bring back `#!/usr/bin/with-contenv sh` in `run.sh`.
- Drop `tini` from apt deps — not needed once s6 is in charge again.
- Python's existing SIGTERM handler still cleans up the ebusd subprocess.

## 0.2.2 — 2026-06-16

- Drop `#!/usr/bin/with-contenv` from `run.sh`. It is an s6-overlay
  helper, but 0.2.0 introduced `tini` as PID 1 (replacing s6-overlay),
  so `/run/s6/container_environment/` no longer exists and the
  container was crashlooping with `s6-envdir: fatal`.

## 0.2.1 — 2026-06-16

- Fix ebusd `.deb` URL: upstream uses `ebusd-26.1_<arch>-bookworm_mqtt1.deb`
  (not `..._debian12_...`), and the current release is 26.1, not 25.1.
  Build was failing with wget exit 8.

## 0.2.0 — 2026-06-16

**Self-contained**: the add-on now bundles `ebusd` and no longer needs
the external `LukasGrebe/ebusd` add-on. Plug the network adapter URL in
the configuration and you are done.

- Switched base image from Alpine to Debian bookworm (Alpine has no
  ebusd package; john30 publishes Debian `.deb` releases).
- `ebusd v25.1` installed from the upstream release `.deb` matching
  the build architecture.
- Vaillant CSV definitions bundled at `/usr/share/ebusd/vaillant/`
  (HMU + CTLS2 + broadcast + VWZ).
- Python now supervises ebusd as a child process: spawn, log forwarding,
  watchdog every 15 s, clean SIGTERM/SIGINT teardown.
- New config options: `ebus_device` (default `ens:192.168.1.171:9999`)
  and `ebusd_log_level`.
- `/api/health` exposes `ebusd_running`, `ebusd_pid`, `ebusd_restarts`.
- New `/api/ebusd` endpoint: `{action: "start"|"stop"|"restart"}`.
- Diagnostics tab shows an ebusd status card and a Restart ebusd button.
- `tini` as PID 1 for proper signal propagation to the multi-process
  container.

Breaking changes: uninstall the LukasGrebe ebusd add-on before
installing this one (or change its MQTT topic prefix) so both daemons
don't publish to the same topics.

Historical-data migration from the old `sensor.ebusd_*` and
`sensor.aerotermia_*` entities is **NOT** part of 0.2.0 — it is
tracked in `LOOP-NOTES.md` for the next session.

## 0.1.4 — 2026-06-13

- UI fully translated to English (the source-of-truth language for the
  project — Spanish copy belongs in a future translations layer).
- Fix Chart.js "Canvas is already in use" error: use `Chart.getChart()`
  to find any chart already attached to the canvas and destroy it
  before rendering, instead of relying on our local CHARTS map.
- Add a CHARTS_BUSY guard so two `loadCharts()` calls (e.g. tab click
  + manual refresh) cannot overlap.
- Chart container heights set via CSS (`.chart-wrap`) instead of being
  patched in JS each render.
- Load `chartjs-adapter-date-fns` so the time-scale X axis renders.

## 0.1.3 — 2026-06-13

- `run.sh` now uses `#!/usr/bin/with-contenv sh` — the HA base-image s6
  init scrubs env for legacy-services so SUPERVISOR_TOKEN never reached
  Python. With `with-contenv` the token is preserved.
- Python falls back to HASSIO_TOKEN if SUPERVISOR_TOKEN is missing.

## 0.1.2 — 2026-06-13

- Retry the supervisor MQTT introspection with backoff (the supervisor
  isn't always ready at the exact moment the add-on starts).
- Print boot diagnostics to stderr before the logger is configured.

## 0.1.1 — 2026-06-13

- Moved supervisor MQTT introspection from `run.sh` (broken under busybox
  + `set -e`) into the Python entrypoint with proper error handling.

## 0.1.0 — 2026-06-13

First standalone add-on release.

- Single-file Flask app with embedded dashboard (5 tabs:
  Estado / Gráficas / Controles / Optimizer / Diagnóstico).
- MQTT subscriber for the LukasGrebe `ebusd` add-on (`ebusd/+/+`).
- Initial sync on boot — force-reads every msg the add-on cares about.
- SQLite at `/data/history.db` for snapshots (per-minute, 12 series).
- Deterministic optimizer: weather-compensated max flow temp,
  seasonal switchover, safety enforcement on user writes, ΔT
  anomaly alert.
- MQTT Discovery publishes 5 entities for HA automations.
- Auth: `X-Ingress-Path` required on all routes,
  `X-Hass-User` required on writes.
