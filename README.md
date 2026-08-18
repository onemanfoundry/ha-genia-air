# Vaillant Genia Air — Home Assistant add-on

[![CI](https://github.com/onemanfoundry/ha-genia-air/actions/workflows/ci.yaml/badge.svg)](https://github.com/onemanfoundry/ha-genia-air/actions/workflows/ci.yaml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

Standalone Home Assistant add-on to **control and optimize** a Vaillant
Genia Air (aroTHERM-class) heat pump over eBUS.

It is **fully self-contained**: it bundles the [`ebusd`](https://github.com/john30/ebusd)
daemon and the Vaillant message definitions, and ships its own dashboard,
history database and optimizer. The only external dependency is the MQTT
integration that Home Assistant already provides.

## Install

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fonemanfoundry%2Fha-genia-air)

Or manually: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, then add:

```
https://github.com/onemanfoundry/ha-genia-air
```

Then install **Vaillant Genia Air** and set `ebus_device` to your adapter.
Full user docs: [`genia_air/README.md`](genia_air/README.md).

> Requires an eBUS adapter wired to the heat pump and reachable over the
> network (e.g. an [eBUS Adapter Shield](https://adapter.ebusd.eu/) on TCP
> `:9999`) or USB, plus an MQTT broker (the Mosquitto add-on is the easiest).

## What you get

- Live KPI dashboard with a zone-1 thermostat:
  - **COP (instantaneous)** = thermal output power ÷ electrical input power,
    read straight off the HMU's own power registers. Falls back to a
    **30-minute rolling COP** (Σ thermal ÷ Σ electric) when the compressor is
    currently idle but ran recently, so the card doesn't just go blank.
  - **ΔT** = supply − return water temperature — the fastest hydraulic
    health signal; sign flips tell heating from cooling.
  - Modulation %, electric/thermal power, flow temps, runtime hours.
- Local SQLite history with charts (6 h / 24 h / 72 h / 7 days).
- A deterministic optimizer: weather-compensated flow-temp curve, seasonal
  heat↔cool switchover, safety clamps and ΔT-anomaly alerts.
- MQTT Discovery device so your automations can hook in.
- Diagnostics tab listing every eBUS message and a force-read trigger.
- **Hardware compatibility check**: Diagnostics compares your unit against
  the exact hardware this add-on is tested on and flags firmware mismatches
  or untested models, with a one-click **🐙 Report on GitHub** that opens a
  pre-filled, anonymized compatibility issue — see below.

> ⚠️ **Tested against exactly one unit**: Vaillant HMU00 SW=0901/HW=5103 +
> CTLS2 SW=0509/HW=1304 + VWZ SW=0522/HW=5103. Other firmware or models may
> work but are unverified — the Diagnostics tab checks your hardware against
> this table and can generate an anonymous compatibility report to help add
> support for yours. Details in [`genia_air/README.md`](genia_air/README.md#compatibility).

## Credits

This add-on bundles [`ebusd`](https://github.com/john30/ebusd) and the
Vaillant eBUS message definitions from
[`ebusd-configuration`](https://github.com/john30/ebusd-configuration) — both
the multi-year work of **John Baier ([@john30](https://github.com/john30))**.
None of this exists without his reverse-engineering and maintenance of the
eBUS protocol. If this add-on is useful to you, consider supporting the
upstream project directly. Full attribution and license details in
[`NOTICE.md`](NOTICE.md).

## Pairs with AI Energy Optimizer

This add-on is the *hands* — it reads live heat-pump telemetry (COP, ΔT,
flow temps) and exposes writable `number.*` / `climate.*` entities via MQTT
Discovery. [**ha-energy-optimizer**](https://github.com/onemanfoundry/ha-energy-optimizer)
is the *brain* — it can use the instantaneous COP and outdoor temperature to
decide *when* to heat, since blindly heating at the cheapest valley-tariff
hours can cost more per useful kWh than heating at midday with a higher COP.
Install this add-on first if your heat pump talks eBUS; the Optimizer's
wizard will then auto-discover the entities it publishes.

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

v0.4.0. Optimizer is deterministic and single-zone focused (ML and
multi-zone are on the roadmap). New in this release: the hardware
compatibility check and anonymized GitHub reporting flow described above —
see [`genia_air/CHANGELOG.md`](genia_air/CHANGELOG.md) for the full history.
Contributions and issue reports welcome.

## License

[Apache-2.0](LICENSE).
