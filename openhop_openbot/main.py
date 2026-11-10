"""openhop-openbot: a standalone MeshCore command bot for openHop Repeater.

Run with no arguments under the openHop plugin manager, which sets
OPENHOP_PLUGIN_DATA to the plugin's data directory; config.json there is what
the plugin's web UI edits, and the bot re-reads it within seconds of a save.

    openhop-openbot                     run the bot
    openhop-openbot --config FILE       run against a specific config.json
    openhop-openbot keygen              new companion identity for config.yaml
    openhop-openbot render "TEMPLATE"   render a reply template offline
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
from pathlib import Path

from . import __version__
from .config import data_dir, default_config_path

logger = logging.getLogger("openbot")


def generate_identity() -> tuple[bytes, bytes]:
    """A MeshCore-compatible Ed25519 keypair: (public_key[32], identity_key[64]).

    The 64-byte form is what the repeater's ``identity_key`` expects: the
    clamped scalar followed by the upper half of sha512(seed), the same
    construction MeshCore firmware and the repeater's own keygen use.
    """
    import hashlib
    import secrets

    from nacl.bindings import crypto_scalarmult_ed25519_base_noclamp

    seed = secrets.token_bytes(32)
    digest = hashlib.sha512(seed).digest()
    scalar = bytearray(digest[:32])
    scalar[0] &= 248
    scalar[31] &= 63
    scalar[31] |= 64
    public_key = crypto_scalarmult_ed25519_base_noclamp(bytes(scalar))
    return public_key, bytes(scalar) + digest[32:64]


def _cmd_keygen(args) -> int:
    pub, key = generate_identity()
    if args.json:
        print(json.dumps({"public_key": pub.hex(), "identity_key": key.hex()}, indent=2))
        return 0
    print("public_key   " + pub.hex())
    print("identity_key " + key.hex())
    print()
    print("Paste identity_key into the repeater's config.yaml under identities.companions.")
    print("It is the bot's PRIVATE key: keep it, and keep it secret.")
    return 0


def _cmd_render(args) -> int:
    """Render a template against an in-memory roster, for trying templates out."""
    from .engine.builtins import BUILTINS
    from .engine.templates import render
    from .store import Store

    store = Store(":memory:")
    ctx = {"store": store, "weather": None, "cfg": {}, "commands": [], "gateway": "openbot",
           "self_pos": None, "tz": "UTC", "units": "imperial", "extra": [],
           "sender": args.sender, "args": args.args, "channel": "#bot", "region": "*",
           "hops_n": 0, "hops": [], "snr": None}
    out = render(args.template, ctx, BUILTINS)
    print(out)
    print("(%d bytes)" % len(out.encode("utf-8")))
    return 0


async def _run(config_path: Path) -> int:
    from .hub import Hub

    hub = Hub(config_path, data_dir() if config_path == default_config_path()
              else config_path.resolve().parent)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, hub.stop)
    await hub.run()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="openhop-openbot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to config.json "
                        "(default: $OPENHOP_PLUGIN_DATA/config.json)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    kg = sub.add_parser("keygen", help="generate a companion identity key")
    kg.add_argument("--json", action="store_true")
    rd = sub.add_parser("render", help="render a reply template offline")
    rd.add_argument("template")
    rd.add_argument("--sender", default="tester")
    rd.add_argument("--args", default="")
    args = parser.parse_args()

    if args.command == "keygen":
        return _cmd_keygen(args)
    if args.command == "render":
        return _cmd_render(args)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    path = Path(args.config) if args.config else default_config_path()
    try:
        return asyncio.run(_run(path))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
