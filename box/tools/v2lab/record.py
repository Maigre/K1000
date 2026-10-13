# SPDX-License-Identifier: GPL-3.0-or-later
"""Record a real protocol-2 hello / state / meters for the v2 lab (box/tools/v2lab).

The frames come from the REAL server (k1000.web.server.PhoneServer) over a real /v2/ws, with the
real Controller on S1's in-memory engine (tests/test_web.py make_ctl), so the fake box replays
exactly what the box sends. Run from box/:

    python tools/v2lab/record.py > tools/v2lab/fixtures.json

test_web_v2_page.py records them again at every run and compares the keys with fixtures.json, so
a protocol change on the box shows up as a failing test, not as a lab that drifts.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

BOX = Path(__file__).resolve().parents[2]
for p in (BOX, BOX / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


async def record(tmp: Path) -> dict[str, Any]:
    from websockets.asyncio.client import connect

    from k1000.web.server import PhoneServer
    from test_web import boot, make_ctl, recv_until, settings

    ctl, _ = make_ctl(tmp)
    await boot(ctl)
    srv = PhoneServer(ctl, settings(tmp), tick_s=0.02)
    await srv.start()
    try:
        port = srv.ports["plain"]
        async with connect(f"ws://127.0.0.1:{port}/v2/ws", origin=f"http://127.0.0.1:{port}") as ws:
            hello = json.loads(await ws.recv())
            state = await recv_until(ws, lambda m: m["t"] == "state")
            meters = await recv_until(ws, lambda m: m["t"] == "m")
    finally:
        await srv.stop()
        await ctl.stop()
    return {"hello": hello, "state": state, "m": meters}


def main() -> None:
    with tempfile.TemporaryDirectory() as d:
        out = asyncio.run(record(Path(d)))
    json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
