#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nudge one field like a phone does (v2 protocol, docs/14), run ON the box.
Usage: v2nudge.py <field> <detents>   (split into messages of at most 4 detents)"""
import asyncio, json, ssl, sys
import websockets

URL = "wss://10.42.0.1/v2/ws"


async def main(field: str, total: int) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(URL, ssl=ctx, origin="https://10.42.0.1", max_size=2**22) as ws:
        await ws.recv()                                     # hello
        await ws.send(json.dumps({"t": "hi", "app": None, "bridge": None, "caps": ["probe"], "id": 1}))
        mid, left = 2, total
        while left:
            d = max(-4, min(4, left))
            await ws.send(json.dumps({"t": "nudge", "f": field, "d": d, "id": mid}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 3))
                if m.get("t") in ("ack", "err") and m.get("id") == mid:
                    print(m.get("t"), json.dumps(m)[:160])
                    break
            left -= d
            mid += 1
            await asyncio.sleep(0.25)


asyncio.run(main(sys.argv[1], int(sys.argv[2])))
