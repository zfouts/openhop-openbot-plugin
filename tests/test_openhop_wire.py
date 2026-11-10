"""Our companion client against openhop_core's real companion frame server.

No radio: the bridge's packet injector captures what the companion would put
on air, so these check the actual on-wire result of our commands, including
that a region-scoped reply leaves as TRANSPORT_FLOOD with the right code.
"""

import asyncio
import socket

import pytest

from openhop_openbot.mesh.companion import CompanionClient
from openhop_openbot.mesh.frames import (
    decode_frame_header,
    frame_region,
    hashtag_secret,
    region_key,
)

core = pytest.importorskip("openhop_core.companion.companion_bridge")
from openhop_core.companion.frame_server.server import CompanionFrameServer  # noqa: E402
from openhop_core.protocol.identity import LocalIdentity  # noqa: E402


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def companion():
    sent = []

    async def injector(pkt, wait_for_ack=False):
        sent.append(pkt.write_to())
        return True

    bridge = core.CompanionBridge(LocalIdentity(), injector, node_name="wirebot")
    port = _free_port()
    server = CompanionFrameServer(bridge, "aa", port=port, bind_address="127.0.0.1")
    await server.start()
    client = CompanionClient("127.0.0.1", port)
    task = asyncio.create_task(client.run())
    for _ in range(100):
        if client.is_connected():
            break
        await asyncio.sleep(0.02)
    assert client.is_connected()
    yield client, sent, bridge
    await client.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await server.stop()


async def test_self_info_parsed(companion):
    client, _, bridge = companion
    assert client.self_info.name == "wirebot"
    assert client.self_info.public_key == bridge.get_public_key()


async def _send_and_capture(client, sent, **kw):
    assert await client.set_channel(1, "#bot", hashtag_secret("#bot"))
    n = len(sent)
    assert await client.send_channel_message(1, "hello", **kw)
    for _ in range(50):
        if len(sent) > n:
            break
        await asyncio.sleep(0.02)
    return decode_frame_header(sent[-1])


async def test_region_scoped_send_is_transport_flood(companion):
    client, sent, _ = companion
    hdr = await _send_and_capture(client, sent, scope=region_key("#tx-dfw"))
    assert hdr["route"] == 0
    assert frame_region(hdr, {"#tx-dfw": region_key("#tx-dfw")}) == "#tx-dfw"
    # the override was cleared: the next plain send is unscoped again
    hdr = await _send_and_capture(client, sent)
    assert hdr["route"] == 1


async def test_forced_unscoped_send(companion):
    client, sent, bridge = companion
    bridge.set_default_flood_scope("#tx-dfw", region_key("#tx-dfw"))
    hdr = await _send_and_capture(client, sent, scope=b"")
    assert hdr["route"] == 1
    hdr = await _send_and_capture(client, sent)      # default scope applies again
    assert hdr["route"] == 0


async def test_set_advert_name_renames_the_companion(companion):
    client, _, bridge = companion
    assert await client.set_advert_name("LibertyHillBot")
    assert bridge.prefs.node_name == "LibertyHillBot"


async def test_set_advert_name_rejects_empty_and_overlong(companion):
    client, _, bridge = companion
    with pytest.raises(ValueError):
        await client.set_advert_name("  ")
    with pytest.raises(ValueError):
        await client.set_advert_name("x" * 32)          # firmware keeps 31 bytes
    assert bridge.prefs.node_name == "wirebot"


@pytest.mark.parametrize("flood,route", [(True, 1), (False, 2)])
async def test_send_self_advert_goes_on_air(companion, flood, route):
    client, sent, _ = companion
    n = len(sent)
    assert await client.send_self_advert(flood=flood)
    for _ in range(50):
        if len(sent) > n:
            break
        await asyncio.sleep(0.02)
    hdr = decode_frame_header(sent[-1])
    assert hdr["payload_type"] == 4                     # ADVERT
    assert hdr["route"] == route                        # 1 = FLOOD, 2 = DIRECT (zero-hop)
