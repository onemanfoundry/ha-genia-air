# NOTICE

This product includes software developed by third parties. The following list
identifies the third-party works and their respective licenses. **You must
honour these attributions and license terms when redistributing this work.**

## eBUS daemon (`ebusd`)

- Author: **John Baier** (@john30)
- Repository: https://github.com/john30/ebusd
- License: **GPL-3.0-only**
- Usage: We **do not embed** any `ebusd` source code. We require the user to
  install the `ebusd` daemon separately (via the LukasGrebe Home Assistant
  add-on) and we interoperate with it via its public MQTT interface.
  This is interoperability, not derivative work.

## eBUS configuration (Vaillant CSVs)

- Authors: John Baier and contributors to https://github.com/john30/ebusd-configuration
- License: **LGPL-3.0+**
- Usage: Files in `custom_components/genia_air/ebusd_csv/` are derivative work
  of the upstream `ebusd-configuration` repository, specifically the Vaillant
  subset. They retain the **LGPL-3.0+** license — see
  `custom_components/genia_air/ebusd_csv/LICENSE`.
- Upstream commit reference: tracked in
  `custom_components/genia_air/ebusd_csv/UPSTREAM_REF.md`

## Home Assistant ebusd Add-on

- Author: **Lukas Grebe** (@LukasGrebe)
- Repository: https://github.com/LukasGrebe/ha-addons
- License: Apache-2.0
- Usage: We **require** users to have this add-on installed. We do not embed
  any of its code. We provide configuration recommendations for it in our
  documentation. Lukas also sells the recommended eBUS hardware adapter; the
  integration's setup flow links to his store as the recommended source.

## genia-air-pack (predecessor, deprecated)

- Author: Sergio Campos García
- Repository: https://github.com/hirofairlane/ha-vaillant-genia-air-pack
- License: MIT (code), LGPL-3.0+ (CSVs)
- Usage: This add-on is the successor of `genia-air-pack`. The same author
  owns both; `genia-air-pack` is being retired in favour of this app. Its
  English entity naming convention is inherited to keep cross-project
  continuity for anyone migrating.

---

## Summary of bundled files by license

| Path | License |
|---|---|
| `custom_components/genia_air/*.py` | Apache-2.0 |
| `custom_components/genia_air/ebusd_csv/*.csv` | **LGPL-3.0+** (NOT Apache) |
| `scripts/*.py` | Apache-2.0 |
| `docs/`, `README.md`, `MIGRATION.md`, `ARCHITECTURE.md` | Apache-2.0 |
| `tests/` | Apache-2.0 |

If you redistribute this work:
1. Keep this `NOTICE.md` intact
2. Keep `custom_components/genia_air/ebusd_csv/LICENSE` intact
3. Honour the LGPL-3.0+ terms for the CSV files (in particular, if you modify
   them, you must release the modifications under the same license and
   credit the upstream authors)
