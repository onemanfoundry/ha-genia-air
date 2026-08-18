# Changelog

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
