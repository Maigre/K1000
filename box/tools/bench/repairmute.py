#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The repair limit and the mute during a setup upload (docs/16 §5.1 #5 and #6), run ON the box.

  repairmute.py repair   CamillaGUI's Apply, repeated: K1000's running config with its first
                         Filter step bypassed, sent with SetConfigJson (what the GUI's Apply
                         sends), sent again each time K1000 restores its pipeline. Expected: 3
                         restores, then the next Apply stays (the alert only). Leaves the engine
                         on the bypassed pipeline: `mute` (or any SETUP action) puts K1000's back.
  repairmute.py mute     As a phone (v2 protocol): SUBS > Mono L+R OFF (a setup action: the
                         pipeline comes back, the repair alert goes), then ON with a MUTE 150 ms
                         later, while the rebuild uploads. Expected: the mute is answered while
                         APPLYING SETUP… is up, and the box is still muted once it is gone. Then
                         unmutes if the box was unmuted before. Needs Mono L+R ON to start.

Exit 0 when the box did what is expected. Main dips (repair) and a mute (mute): speakers off.
"""
import asyncio
import json
import ssl
import sys
import time
from datetime import datetime

import websockets

ENGINE = "ws://127.0.0.1:1234"
PHONE = "wss://10.42.0.1/v2/ws"
WAIT_RESTORE_S = 50.0   # RESTORE_MIN_INTERVAL_S (30 s) plus K1000's two 1 s looks
WAIT_STAYS_S = 75.0     # after the 4th Apply: no restore for this long
SETUP_MSG = "APPLYING SETUP…"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------- repair: the engine websocket
async def engine(ws, cmd, value=None):
    """CamillaDSP 5: {"command": NAME[, "value": …]} -> {"reply": NAME, "result", "value"}."""
    msg = {"command": cmd} if value is None else {"command": cmd, "value": value}
    await ws.send(json.dumps(msg))
    while True:
        reply = json.loads(await ws.recv())
        if reply.get("reply") in (cmd, "Invalid"):
            break
    if reply.get("result") != "Ok":
        raise RuntimeError(f"{cmd}: {reply}")
    return reply.get("value")


async def pipeline(ws):
    return json.loads(await engine(ws, "GetConfigJson"))["pipeline"]


async def repair() -> int:
    async with websockets.connect(ENGINE, max_size=None) as ws:
        cfg = json.loads(await engine(ws, "GetConfigJson"))
        idx = next(i for i, s in enumerate(cfg["pipeline"]) if s.get("type") == "Filter")
        print(f"{ts()} bypassing pipeline step {idx} of {len(cfg['pipeline'])}: "
              f"{json.dumps(cfg['pipeline'][idx])[:160]}", flush=True)
        cfg["pipeline"][idx]["bypassed"] = True
        sent = json.dumps(cfg)
        restores = 0
        for n in range(1, 6):
            await engine(ws, "SetConfigJson", sent)
            t0 = time.monotonic()
            ours = await pipeline(ws)   # the engine's read-back; K1000 needs two looks first
            if not ours[idx].get("bypassed"):
                raise RuntimeError(f"the engine did not keep the bypass: {ours[idx]}")
            print(f"{ts()} Apply #{n}", flush=True)
            limit = WAIT_STAYS_S if n > 3 else WAIT_RESTORE_S
            while time.monotonic() - t0 < limit:
                await asyncio.sleep(0.5)
                if await pipeline(ws) != ours:
                    restores += 1
                    print(f"{ts()}   restored by K1000 after {time.monotonic() - t0:.1f} s",
                          flush=True)
                    break
            else:
                print(f"{ts()}   stays: no restore in {limit:.0f} s", flush=True)
                break
        ok = restores == 3 and n == 4
        print(f"RESULT {restores} restores, Apply #{n} stays: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1


# ---------------------------------------------------------------- mute: the phone protocol
class Phone:
    def __init__(self, ws):
        self.ws, self.state, self.ready = ws, {}, asyncio.Event()
        self.waiting: dict = {}
        self.setup_gone: float | None = None   # monotonic time APPLYING SETUP… left the alerts
        self.watch = False

    async def read(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("t") == "state":
                was = self.state.get("alerts") or []
                self.state = m
                self.ready.set()
                now_up = any(a[1] == SETUP_MSG for a in m.get("alerts") or [])
                if self.watch and any(a[1] == SETUP_MSG for a in was) and not now_up:
                    self.setup_gone = time.monotonic()
                    print(f"{ts()}   {SETUP_MSG} gone; muted={m.get('muted')} "
                          f"src={m.get('mute_src')!r} band={m.get('mute_band')}", flush=True)
                elif self.watch and now_up and not any(a[1] == SETUP_MSG for a in was):
                    print(f"{ts()}   {SETUP_MSG} up; muted={m.get('muted')}", flush=True)
            elif m.get("t") in ("ack", "err"):
                fut = self.waiting.pop(m.get("id"), None)
                if fut and not fut.done():
                    fut.set_result((time.monotonic(), ts(), m))

    async def send(self, msg: dict):
        fut = asyncio.get_running_loop().create_future()
        self.waiting[msg["id"]] = fut
        await self.ws.send(json.dumps(msg))
        return fut


def view(st: dict) -> str:
    return (f"muted={st.get('muted')} src={st.get('mute_src')!r} "
            f"mono={st.get('setup', {}).get('mono_subs')} alerts={st.get('alerts')}")


async def mute() -> int:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    async with websockets.connect(PHONE, ssl=ctx, origin="https://10.42.0.1",
                                  max_size=2**22) as ws:
        ph = Phone(ws)
        reader = asyncio.create_task(ph.read())
        await ws.send(json.dumps({"t": "hi", "app": None, "bridge": None, "caps": ["probe"],
                                  "id": 1}))
        await asyncio.wait_for(ph.ready.wait(), 5)
        await asyncio.sleep(0.5)
        muted0 = ph.state.get("muted")
        print(f"{ts()} start: {view(ph.state)}", flush=True)
        if ph.state.get("setup", {}).get("mono_subs") is not True:
            print("Mono L+R is not ON: set it ON first")
            return 2

        fut = await ph.send({"t": "choose", "f": "mono_subs", "v": False, "seen": True,
                             "id": "off"})
        _, at, ack = await asyncio.wait_for(fut, 20)
        print(f"{at} Mono L+R OFF: {ack.get('t')} {ack.get('v')}", flush=True)
        await asyncio.sleep(3)
        print(f"{ts()} after it: {view(ph.state)}", flush=True)
        cleared = not ph.state.get("alerts")

        ph.watch = True
        fut_on = await ph.send({"t": "choose", "f": "mono_subs", "v": True, "seen": False,
                                "id": "on"})
        await asyncio.sleep(0.15)
        t_sent = time.monotonic()
        print(f"{ts()} Mono L+R ON sent, MUTE sent", flush=True)
        fut_m = await ph.send({"t": "mute", "on": True, "id": "mute"})
        t_ack, at, ack = await asyncio.wait_for(fut_m, 10)
        print(f"{at}   mute {ack.get('t')} {ack.get('v')} after "
              f"{(t_ack - t_sent) * 1000:.0f} ms", flush=True)
        await asyncio.wait_for(fut_on, 20)
        await asyncio.sleep(3)
        ph.watch = False
        end = dict(ph.state)
        print(f"{ts()} after it: {view(end)}", flush=True)
        during = ph.setup_gone is not None and t_ack < ph.setup_gone
        stayed = end.get("muted") is True and end.get("hold") == "unmute"

        if muted0 is False and end.get("muted") is True:
            fut = await ph.send({"t": "mute", "on": False, "seq": end.get("mute_seq"),
                                 "id": "unmute"})
            _, at, ack = await asyncio.wait_for(fut, 10)
            print(f"{at} back as found: unmute {ack.get('t')} {ack.get('v')}", flush=True)
        reader.cancel()
        ok = cleared and during and stayed
        print(f"RESULT alert cleared by the setup action: {cleared}; mute answered during the "
              f"upload: {during}; still muted after it: {stayed}: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("repair", "mute"):
        sys.exit(__doc__)
    sys.exit(asyncio.run(repair() if mode == "repair" else mute()))
