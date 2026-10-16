#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only probe of the box's RESPONSE side (docs/14 §3.6), run ON the box: connect to
/v2/ws like a phone, check hello.pgm, sub {pgm: true}, read frames for N seconds, unsubscribe.
Sends nothing but hi, sub and unsub: no edit, no mute."""
import asyncio, base64, json, ssl, sys, time
import websockets

URL = "wss://10.42.0.1/v2/ws"
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0


async def main() -> None:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(URL, ssl=ctx, origin="https://10.42.0.1", max_size=2**20) as ws:
        hello = json.loads(await ws.recv())
        print("hello.t", hello.get("t"), "v", hello.get("v"), "pgm", json.dumps(hello.get("pgm")))
        await ws.send(json.dumps({"t": "hi", "app": None, "bridge": None, "caps": ["probe"], "id": 1}))
        await ws.send(json.dumps({"t": "sub", "pgm": True, "id": 2}))
        t0 = time.monotonic()
        frames, acks, st = [], [], {}
        while time.monotonic() - t0 < SECS:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
            except asyncio.TimeoutError:
                continue
            t = m.get("t")
            if t in ("ack", "err"):
                acks.append(m)
            elif t == "pgm":
                frames.append(m)
            elif t == "state":
                st = m
        await ws.send(json.dumps({"t": "sub", "pgm": False, "id": 3}))
        await asyncio.sleep(0.3)
    print("acks", json.dumps(acks)[:400])
    live = [f for f in frames if f.get("st") == "live"]
    print("pgm frames", len(frames), "live", len(live), "states", sorted({f.get("st") for f in frames}))
    for f in frames[:3] + frames[-2:]:
        m = f.get("m")
        db = None
        if m:
            raw = base64.b64decode(m)
            v = [int.from_bytes(raw[i:i + 2], "little", signed=True) / 100 for i in range(0, len(raw), 2)]
            db = (len(v), round(max(v), 1), round(sum(v) / len(v), 1))
        print({k: f.get(k) for k in ("seq", "st", "n", "k", "dt", "prog")}, "m(n,max,mean dB)", db)
    if st:
        print("state.sys_eq", st.get("sys_eq"))


asyncio.run(main())
