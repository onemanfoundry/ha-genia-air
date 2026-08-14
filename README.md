# Vaillant Genia Air — Home Assistant add-on

[![CI](https://github.com/onemanfoundry/genia-air-ha/actions/workflows/ci.yaml/badge.svg)](https://github.com/onemanfoundry/genia-air-ha/actions/workflows/ci.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Standalone Home Assistant add-on to **control and optimize** a Vaillant
Genia Air (aroTHERM-class) heat pump over eBUS.

It is **fully self-contained**: it bundles the [`ebusd`](https://github.com/john30/ebusd)
daemon and the Vaillant message definitions, and ships its own dashboard,
history database and optimizer. The only external dependency is the MQTT
integration that Home Assistant already provides.

## Install

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fonemanfoundry%2Fgenia-air-ha)

Or manually: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, then add:

```
https://github.com/onemanfoundry/genia-air-ha
```

Then install **Vaillant Genia Air** and set `ebus_device` to your adapter.
Full user docs: [`genia_air/README.md`](genia_air/README.md).

> Requires an eBUS adapter wired to the heat pump and reachable over the
> network (e.g. an [eBUS Adapter Shield](https://adapter.ebusd.eu/) on TCP
> `:9999`) or USB, plus an MQTT broker (the Mosquitto add-on is the easiest).

## What you get

- Live KPI dashboard (ΔT, COP, modulation, electric/thermal power, flow
  temps, runtime hours) with a zone-1 thermostat.
- Local SQLite history with charts (6 h / 24 h / 72 h / 7 days).
- A deterministic optimizer: weather-compensated flow-temp curve, seasonal
  heat↔cool switchover, safety clamps and ΔT-anomaly alerts.
- MQTT Discovery device so your automations can hook in.
- Diagnostics tab listing every eBUS message and a force-read trigger.

## Supported architectures

`aarch64` · `amd64` (bundled ebusd `.deb` per arch). 32-bit arches were
dropped in line with Home Assistant 2025.12 deprecating them.

## Repository layout

| Path | Purpose |
|---|---|
| `genia_air/` | The add-on (config.yaml, Dockerfile, source) — this is what gets distributed |
| `repository.yaml` | Add-on store entry |
| `ARCHITECTURE.md` | Design notes & rationale |
| `tests/` | Regression suite (run in CI) |
| `_reference/` | Archived HACS custom integration, kept only as eBUS-parser reference |

## Status

v0.3.0 — first community release. Optimizer is deterministic and
single-zone focused (ML and multi-zone are on the roadmap). Contributions
and issue reports welcome.

## License

[MIT](LICENSE).
