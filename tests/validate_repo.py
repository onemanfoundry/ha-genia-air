#!/usr/bin/env python3
"""Static repo validation used by CI (and runnable locally).

Checks:
  * the add-on / repository YAML files parse,
  * config.yaml `version` matches the VERSION constant in genia_air.py,
  * genia_air.py carries no Spanish-only characters (the UI and HA
    notifications are English-only; this catches leftover strings).

Exit non-zero on any failure.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    errors: list[str] = []

    for rel in ("genia_air/config.yaml", "genia_air/build.yaml", "repository.yaml"):
        try:
            yaml.safe_load((ROOT / rel).read_text())
            print(f"OK   yaml {rel}")
        except Exception as exc:  # noqa: BLE001 - report any parse error
            errors.append(f"YAML parse failed for {rel}: {exc}")

    cfg_version = yaml.safe_load((ROOT / "genia_air/config.yaml").read_text())["version"]
    src = (ROOT / "genia_air/rootfs/usr/bin/genia_air.py").read_text()
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', src)
    src_version = m.group(1) if m else None
    if cfg_version == src_version:
        print(f"OK   version in sync: {cfg_version}")
    else:
        errors.append(f"version mismatch: config.yaml={cfg_version} genia_air.py={src_version}")

    es_lines = [n for n, line in enumerate(src.splitlines(), 1) if re.search(r"[áéíóúñÁÉÍÓÚÑ¿¡]", line)]
    if es_lines:
        errors.append(f"non-English characters in genia_air.py at lines {es_lines}")
    else:
        print("OK   genia_air.py is English-only")

    if errors:
        print("\nFAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nAll static checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
