# SPDX-License-Identifier: GPL-3.0-or-later
"""The v2 phone page (static/v2/, docs/15-phone-v2-page.md) against its contract (docs/14).

Python-only checks always run: the page keeps the box's CSP (no inline script or style), every
module it imports exists, the service worker's shell list is exactly the page's files, the box
stamps its build into every module, and the lab's recorded hello / state still match what the
real server sends.

The browser checks run when node is present (and Playwright for the e2e ones), skipped
otherwise (the box itself has no node):
- tools/v2lab/unit.mjs: the page's pure modules under node (step arithmetic, speed mapping,
  stepper labels, the PA-up mirror, the nudge batches);
- tools/v2lab/e2e.mjs: headless Chromium against the fake protocol-2 box (fit, gestures, mute,
  two fingers, choose-then-set, ALIGN lock, notch helper, a stalled link, link loss, odd box
  messages, box-update reload, spectrum subscription, the APK bridge, no console error);
- tools/v2lab/real.mjs: the page against the REAL server started here in-process (the real CSP,
  build stamp and /v2/ws): hello and state render, a nudge and a band IN/OUT make the round trip.

Playwright: K1000_PLAYWRIGHT=/path/to/node_modules/playwright, or `npm i` in tools/v2lab.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from k1000.web import server as web
from k1000.web.server import PhoneServer, load_static_all
from test_web import boot, make_ctl, run, settings

BOX = Path(__file__).resolve().parents[1]
LAB = BOX / "tools" / "v2lab"
V2 = web.STATIC_DIR / "v2"
NODE = shutil.which("node")


def _playwright() -> str | None:
    env = os.environ.get("K1000_PLAYWRIGHT")
    if env and Path(env).exists():
        return env
    if NODE is None:
        return None
    r = subprocess.run([NODE, "-e", "console.log(require.resolve('playwright'))"], cwd=LAB,
                       capture_output=True, text=True, timeout=30)
    return r.stdout.strip() or None if r.returncode == 0 else None


PLAYWRIGHT = _playwright()
needs_node = pytest.mark.skipif(NODE is None, reason="needs node (box/tools/v2lab)")
needs_browser = pytest.mark.skipif(PLAYWRIGHT is None, reason="needs node + Playwright "
                                   "(K1000_PLAYWRIGHT=/path/to/node_modules/playwright)")


def _record(tmp: Path) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("v2lab_record", LAB / "record.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return run(mod.record(tmp))


def _node(script: str, *args: str, timeout: float) -> dict[str, Any]:
    env = dict(os.environ)
    if PLAYWRIGHT:
        env["K1000_PLAYWRIGHT"] = PLAYWRIGHT
    r = subprocess.run([NODE or "node", str(LAB / script), *args], cwd=BOX, env=env,
                       capture_output=True, text=True, timeout=timeout)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    assert lines, f"{script}: no result (exit {r.returncode})\n{r.stderr[-3000:]}"
    return json.loads(lines[-1])


def _failed(res: dict[str, Any]) -> str:
    return "\n".join(f"- {c['name']}: {c['detail'][:400]}" for c in res["checks"] if not c["ok"])


# ============================================================================ static, python only
def _v2_files() -> list[str]:
    return sorted(p.relative_to(V2).as_posix() for p in V2.rglob("*")
                  if p.is_file() and not any(part.startswith(".") for part in p.relative_to(V2).parts))


def test_v2_page_keeps_the_csp() -> None:
    """script-src 'self'; style-src 'self' (docs/14 §1): no inline script, no <style>, no style=
    attribute, no eval; every script is a module from the box."""
    html = (V2 / "index.html").read_text()
    assert "K1000 v2" in html                         # test_web_v2's static check reads it
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script>"
    assert "<style" not in html and not re.search(r"\sstyle\s*=", html), "inline style"
    assert re.search(r'<script type="module" src="js/main.js"></script>', html)
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    for js in sorted(V2.rglob("*.js")):
        src = js.read_text()
        assert "eval(" not in src and "new Function" not in src, js.name
        # markup written from JS goes through innerHTML only with our own literals: never a
        # style= attribute (the CSP drops it) nor an inline handler
        assert not re.search(r"""['"`][^'"`\n]*\sstyle\s*=""", src), f"{js.name}: style= in markup"
        assert not re.search(r"""['"`][^'"`\n]*<[a-z][^'"`\n]*\son[a-z]+\s*=""", src), f"{js.name}: inline handler"


def test_v2_modules_resolve_and_the_worker_caches_exactly_the_page() -> None:
    files = set(_v2_files())
    for js in sorted((V2 / "js").glob("*.js")):
        for imp in re.findall(r"""(?:import|export)[^'"]*?from\s+["'](\.[^"']+)["']|import\s+["'](\.[^"']+)["']""", js.read_text()):
            target = (js.parent / (imp[0] or imp[1])).resolve()
            assert target.is_file(), f"{js.name} imports {imp}"
    sw = (V2 / "sw.js").read_text()
    shell = re.search(r"const SHELL = \[(.*?)\];", sw, re.S)
    assert shell
    listed = {s for s in re.findall(r'"([^"]+)"', shell.group(1))}
    want = {"./"} | {f for f in files if f not in ("index.html", "sw.js")}
    assert listed == want, f"sw.js SHELL: missing {sorted(want - listed)}, extra {sorted(listed - want)}"


def test_the_box_stamps_its_v2_build_into_every_module() -> None:
    assets, builds = load_static_all(web.STATIC_DIR)
    b2 = builds["v2"]
    for f in _v2_files():
        if f.endswith(".js"):
            body = assets["/v2/" + f].body
            assert b"__K1000_BUILD__" not in body, f
    for f in ("js/link.js", "sw.js"):
        assert assets["/v2/" + f].body.decode().count(b2) == 1, f


def test_lab_fixtures_are_what_the_box_sends(tmp_path: Path) -> None:
    """tools/v2lab/fixtures.json (what the fake box replays) has the keys the real server sends
    today: a protocol change shows up here, not as a lab that drifts."""
    fresh = _record(tmp_path)
    saved = json.loads((LAB / "fixtures.json").read_text())
    for k in ("hello", "state", "m"):
        assert set(saved[k]) == set(fresh[k]), (k, set(saved[k]) ^ set(fresh[k]))
    assert set(saved["hello"]["fields"]) == set(fresh["hello"]["fields"])
    assert saved["hello"]["slide"] == fresh["hello"]["slide"]
    assert saved["hello"]["nudge"] == fresh["hello"]["nudge"]
    assert [g["group"] for g in saved["hello"]["setup"]] == [g["group"] for g in fresh["hello"]["setup"]]
    assert set(saved["state"]["setup"]) == set(fresh["state"]["setup"])


# ============================================================================ node / Chromium
@needs_node
def test_v2_pure_modules_under_node(tmp_path: Path) -> None:
    fx = tmp_path / "fixtures.json"
    fx.write_text(json.dumps(_record(tmp_path / "rec")))
    res = _node("unit.mjs", str(fx), timeout=60)
    assert res["ok"], _failed(res)


@needs_browser
def test_v2_page_e2e_against_the_fake_box(tmp_path: Path) -> None:
    fx = tmp_path / "fixtures.json"
    fx.write_text(json.dumps(_record(tmp_path / "rec")))
    res = _node("e2e.mjs", str(fx), timeout=900)
    assert res["ok"], f"{res.get('failed')} of {res.get('n')} failed:\n" + _failed(res)
    assert res["n"] >= 400                            # (a run that silently skipped a part)


@needs_browser
def test_v2_page_end_to_end_with_the_real_server(tmp_path: Path) -> None:
    """The real PhoneServer (S1) with the real Controller on the in-memory engine, in this
    process; Chromium loads /v2/ from it: hello and state render, a stepper nudge and a chip
    hold reach the Controller, the worker installs, no console error under the real CSP."""
    async def go() -> tuple[Any, ...]:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        try:
            port = srv.ports["plain"]
            m0 = ctl.state.master_db
            env = dict(os.environ, K1000_PLAYWRIGHT=PLAYWRIGHT or "")
            proc = await asyncio.create_subprocess_exec(
                NODE or "node", str(LAB / "real.mjs"), f"http://127.0.0.1:{port}/v2/", cwd=str(BOX), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 150)
            return (m0, proc.returncode, out.decode(), err.decode(), ctl.state.master_db,
                    ctl.state.live_peq[2].on, dict(srv.rx2))
        finally:
            await srv.stop()
            await ctl.stop()

    m0, rc, out, err, master, on, rx = run(go(), limit=200)
    lines = [ln for ln in out.splitlines() if ln.startswith("{")]
    assert lines, err[-3000:]
    res = json.loads(lines[-1])
    assert res["ok"] and rc == 0, _failed(res)
    assert master == pytest.approx(m0 - 0.5)          # the Controller moved, by one step
    assert on is False                                 # D-014 through the chip hold
    assert rx.get("nudge") == 1 and rx.get("band") == 1 and rx.get("hi", 0) >= 1 and rx.get("sub", 0) >= 1, rx
