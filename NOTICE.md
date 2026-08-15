# NOTICE

This product includes software developed by third parties. The following list
identifies the third-party works and their respective licenses. **You must
honour these attributions and license terms when redistributing this work.**

## eBUS daemon (`ebusd`)

- Author: **John Baier** (@john30)
- Repository: https://github.com/john30/ebusd
- License: **GPL-3.0-only**
- Usage: We **embed the official pre-built `.deb` binary**, downloaded
  unmodified from the upstream GitHub Releases page at image build time (see
  `Dockerfile`, `EBUSD_VERSION`) and installed into the add-on's Docker
  image. We do not modify or redistribute `ebusd` source. Source for the
  exact bundled version is publicly available at the repository above under
  the matching release tag — this satisfies GPL-3.0's source-availability
  requirement via upstream's own public distribution; we do not host a
  separate source mirror.

## eBUS configuration (Vaillant CSVs)

- Authors: John Baier and contributors to https://github.com/john30/ebusd-configuration
- License: **LGPL-3.0+**
- Usage: Files in `genia_air/rootfs/usr/share/ebusd/vaillant/` are derivative
  work of the upstream `ebusd-configuration` repository, specifically the
  Vaillant subset, and retain the **LGPL-3.0+** license.
- `LICENSE` and `UPSTREAM_REF.md` ship alongside the CSVs at
  `genia_air/rootfs/usr/share/ebusd/vaillant/`, pinning the exact upstream
  snapshot (extracted from `john30/ebusd:v23.1`, 2026-05-03, `76.vwz.csv`
  includes upstream PR #330). The same snapshot is also distributed by the
  sibling (deprecated) `ha-vaillant-genia-air-pack` project — see its
  `share/vaillant/` for the original packaging.

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
| `genia_air/rootfs/usr/bin/*.py` | Apache-2.0 (this repo's [`LICENSE`](LICENSE)) |
| `genia_air/rootfs/usr/share/ebusd/vaillant/*.csv` | **LGPL-3.0+** (NOT Apache) |
| `ebusd` binary (embedded in the Docker image) | **GPL-3.0-only** (not modified, see above) |
| `scripts/*.py`, `tests/` | Apache-2.0 |
| `docs/`, `README.md`, `MIGRATION.md`, `ARCHITECTURE.md` | Apache-2.0 |
| `_reference/` (archived HACS integration, not distributed) | Apache-2.0 — historical only, does not apply to the add-on image |

If you redistribute this work:
1. Keep this `NOTICE.md` intact
2. Ship a `LICENSE` file alongside the CSVs in
   `genia_air/rootfs/usr/share/ebusd/vaillant/` (currently missing — see the
   compliance gap noted above)
3. Honour the LGPL-3.0+ terms for the CSV files (in particular, if you modify
   them, you must release the modifications under the same license and
   credit the upstream authors)
4. Honour GPL-3.0 for the embedded `ebusd` binary — don't strip or obscure
   its origin; point recipients at the upstream release for source
