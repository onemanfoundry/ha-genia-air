# Upstream provenance and modifications

The CSV files in this directory are derivative work of
[`github.com/john30/ebusd-configuration`](https://github.com/john30/ebusd-configuration),
licensed under [LGPL-3.0-or-later](LICENSE).

The original project is the multi-year, mostly-solo work of **John Baier (@john30)**.
If you find these definitions useful, the upstream project is where to send
your appreciation, bug reports, contributions and sponsorship.

## Files in this directory

| File | Vaillant device address | Tested firmware |
|---|---|---|
| `08.hmu.csv` | `08` — Heat Management Unit (HMU) | `MF=Vaillant;ID=HMU00;SW=0901;HW=5103` |
| `15.ctls2.csv` | `15` — Sigma 2 controller (CTLS2) | `MF=Vaillant;ID=CTLS2;SW=0509;HW=1304` |
| `76.vwz.csv` | `76` — Compressor module (VWZ) | `MF=Vaillant;SW=0522;HW=5103` |
| `broadcast.csv` | broadcast address `fe` | n/a |

## Provenance summary

The files were extracted from a working configuration of
`john30/ebusd:v23.1` Docker container as of **2026-05-03**, running on a
real Genia Air installation (Madrid, Spain). Each file carries a header
comment recording this extraction. They were copied over unchanged when
this add-on became self-contained (commit `dd89b83`, v0.2.0) and now live
at `genia_air/rootfs/usr/share/ebusd/vaillant/` inside the distributed
Docker image.

The contents are **not bit-for-bit identical** to any single upstream file
in `ebusd-configuration`:

- `76.vwz.csv` incorporates **additions from upstream PR #330**
  (`for HMU;0901;5103 / VWZ;0522;5103`), reflecting register definitions
  that improve VWZ coverage on this firmware revision.
- Other files may diverge from upstream HEAD if specific registers had to
  be redefined to match the firmware actually seen on the test unit.

The same snapshot is also distributed, unmodified, by the sibling
(deprecated) project
[`ha-vaillant-genia-air-pack`](https://github.com/hirofairlane/ha-vaillant-genia-air-pack) —
see that repo's `share/vaillant/` for the original packaging this was
copied from.

## Adapting for your own unit

If your hardware reports a different `MF=…;SW=…;HW=…` string at boot,
expect some registers to parse incorrectly (`ERR: invalid position` in
`ebusd` logs). This is non-fatal — `ebusd` will skip the failing register
and continue publishing the rest.

We strongly recommend checking the canonical upstream first:
[`john30/ebusd-configuration`](https://github.com/john30/ebusd-configuration).
That repository has actively-maintained configurations for the entire
Vaillant range; the files here are simply a frozen snapshot known to work
on a specific installation.

If you make improvements that are not specific to your installation, please
consider contributing them **upstream** rather than just here — that way the
whole community benefits.
