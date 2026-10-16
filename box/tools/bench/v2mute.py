#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Mute or unmute like a phone (v2 protocol, docs/14), run ON the box. Usage: v2mute.py on|off"""
import asyncio, json, ssl, sys, time
import websockets

URL = "wss://10.42.0.1/v2/ws"


async def main(on: bool) -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(URL, ssl=ctx, origin="https://10.42.0.1", max_size=2**22) as ws:
        seq = None

        def find(o):
            if isinstance(o, dict):
                if isinstance(o.get("mute_seq"), int):
                    return o["mute_seq"]
                for v in o.values():
                    r = find(v)
                    if r is not None:
                        return r
            return None
        seq = find(json.loads(await ws.recv()))
        await ws.send(json.dumps({"t": "hi", "app": None, "bridge": None, "caps": ["probe"], "id": 1}))
        while seq is None:
            seq = find(json.loads(await asyncio.wait_for(ws.recv(), 3)))
        print("mute_seq", seq)
        await ws.send(json.dumps({"t": "mute", "on": on, "seq": seq, "id": 2}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 3))
            if m.get("t") in ("ack", "err") and m.get("id") == 2:
                print(m.get("t"), json.dumps(m)[:160])
                break


asyncio.run(main(sys.argv[1] == "on"))
