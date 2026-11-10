#!/usr/bin/env python3
"""Stamp one version into every place the plugin records it.

    python scripts/set_version.py 1.0.0+edge.42

Rewrites pyproject.toml [project].version, openhop-plugin.json "version" and
openhop_openbot/__init__.py __version__. The version must satisfy the openHop
manifest regex. Fails if any location cannot be found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The regex openHop validates the manifest version with.
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")

# (file, pattern with the version in group 2, between groups 1 and 3)
TARGETS = [
    ("pyproject.toml", re.compile(r'(?m)^(version = ")([^"]*)(")$')),
    ("openhop-plugin.json", re.compile(r'(?m)^(  "version": ")([^"]*)(",)$')),
    ("openhop_openbot/__init__.py", re.compile(r'(?m)^(__version__ = ")([^"]*)(")$')),
]


def set_version(version: str, root: Path = ROOT) -> None:
    if not VERSION_RE.match(version):
        raise SystemExit(f"invalid version {version!r}: does not match the openHop manifest regex")
    updates = []
    for rel, pattern in TARGETS:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        matches = pattern.findall(text)
        if len(matches) != 1:
            raise SystemExit(f"{rel}: expected exactly one version line, found {len(matches)}")
        updates.append((path, pattern.sub(lambda m: m.group(1) + version + m.group(3), text)))
    for path, text in updates:
        path.write_text(text, encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: set_version.py <version>")
    set_version(sys.argv[1])
    print(f"version set to {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
