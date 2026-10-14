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
- tools/v2lab/micunit.mjs: the phone-mic maths under node (micmath.js: the night branch's RTA
  maths, the engine's estimator against a numpy port of CamillaDSP's spectrum.rs
  (spectrum_ref.json), matched vs mismatched estimators, the box's PEAK rule against the box's
  own cases and on the engine's numbers, the program gate, the `pgm` codec, the RESPONSE
  windows, the master divided out, the clip flag);
- tools/v2lab/e2e.mjs: headless Chromium against the fake protocol-2 box (fit, gestures, mute,
  two fingers, choose-then-set, ALIGN lock, notch helper, a stalled link, link loss, odd box
  messages, box-update reload, spectrum subscription, the APK bridge, no console error), and
  full Chromium with a fake mic playing WAVs made at run time (the mic asked only from SETUP,
  the line, its PEAK and the notch on it (taken at touch-down), the tracks stopped, MEASURE,
  RESPONSE against the fake box's `pgm` windows, the clip flag, audio never leaving the phone,
  Android's mic dialog hiding the page while the request is pending);
- tools/v2lab/real.mjs: the page against the REAL server started here in-process (the real CSP,
  build stamp and /v2/ws): hello and state render, a nudge and a band IN/OUT make the round trip;
  then RESPONSE end to end: the production spectrum task (`__main__.open_spectrum`) on a fake
  engine that plays a pink noise in real time (GetSpectrum with the engine's estimator, the
  program level), the real accumulator and `pgm` frames, and full Chromium whose fake mic plays
  the same noise: the page keeps the windows, the coverage grows, RESPONSE reads flat.

Playwright: K1000_PLAYWRIGHT=/path/to/node_modules/playwright, or `npm i` in tools/v2lab.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from k1000.cdsp import CdspError, CdspUnavailable
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


@needs_node
def test_v2_phone_mic_maths_under_node() -> None:
    res = _node("micunit.mjs", timeout=120)
    assert res["ok"], _failed(res)
    assert res["n"] >= 47                             # (a run that silently skipped a part)


@needs_browser
def test_v2_page_e2e_against_the_fake_box(tmp_path: Path) -> None:
    fx = tmp_path / "fixtures.json"
    fx.write_text(json.dumps(_record(tmp_path / "rec")))
    res = _node("e2e.mjs", str(fx), timeout=900)
    assert res["ok"], f"{res.get('failed')} of {res.get('n')} failed:\n" + _failed(res)
    assert res["n"] >= 575                            # (a run that silently skipped a part)
    names = {c["name"].split(":")[0] for c in res["checks"]}
    assert {"mic", "micpeak", "micpeakhf", "resp", "resptilt", "respclip", "micdeny", "micdialog", "measure"} <= names, names   # the fake-mic parts ran


# ============================================================================ the real round trip
RATE = 48000


def pink_wav(path: Path, n: int = 1 << 19, seed: int = 1000) -> np.ndarray:
    """A pink noise (power ~ 1/f from 15 Hz: flat band power), 48 kHz mono 16-bit, 10.9 s,
    made in the frequency domain so its loop has no click (as wav.mjs). Written to `path` for
    Chromium's fake mic; returned as the samples it plays (the fake engine plays them too)."""
    rng = np.random.default_rng(seed)
    f = np.arange(n // 2 + 1) * RATE / n
    a = np.where(f < 15.0, 0.0, 1.0 / np.sqrt(np.maximum(f, 1e-9)))
    a[-1] = 0.0
    x = np.fft.irfft((rng.standard_normal(len(f)) + 1j * rng.standard_normal(len(f))) * a, n)
    pcm = np.round(x * (0.5 / np.max(np.abs(x))) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return pcm.astype(np.float32) / 32768.0


def engine_spectrum(sig: np.ndarray, f0: float, f1: float, nb: int) -> tuple[list[float], list[float]]:
    """CamillaDSP 5.0.0-beta.1's GetSpectrum (spectrum.rs) on `sig` (its FFT length): Hann,
    power scaled 4x except DC and Nyquist, over the window's sum squared, each band = the PEAK
    FFT bin between the geometric midpoints. tools/v2lab/spectrum_ref.py vectorised (that port
    is checked against the page's estimator by micunit.mjs)."""
    n = len(sig)
    w = (0.5 * (1.0 - np.cos(2.0 * math.pi * np.arange(n) / (n - 1)))).astype(np.float32)
    inv_w2 = 1.0 / float(np.sum(w.astype(np.float64))) ** 2
    spec = np.fft.rfft((sig.astype(np.float32) * w).astype(np.float32))
    p = (spec.real ** 2 + spec.imag ** 2) * inv_w2 * 4.0
    p[0] /= 4.0
    p[-1] /= 4.0
    res, ratio = RATE / n, (f1 / f0) ** (1.0 / (nb - 1))
    fc = f0 * ratio ** np.arange(nb)
    lo = np.concatenate(([f0], fc[1:] / math.sqrt(ratio)))
    hi = np.concatenate((fc[:-1] * math.sqrt(ratio), [f1]))
    kl = np.floor(lo / res).astype(int)
    kh = np.minimum(np.ceil(hi / res).astype(int), len(p) - 1)
    out = np.array([p[a:b + 1].max() if a <= b else p[min(int(fc[i] / res + 0.5), len(p) - 1)]
                    for i, (a, b) in enumerate(zip(kl, kh))])
    return fc.tolist(), (10.0 * np.log10(np.maximum(out, 1e-30))).tolist()


class PlayingEngine:
    """The spectrum task's OWN engine client (the CamillaClient surface it uses): CamillaDSP
    playing `x` on every capture channel in real time. GetSpectrum = the engine's estimator on
    the last FFT-length samples; GetCaptureSignalRmsSince = the program present (-20 dBFS). The
    controller keeps its in-memory FakeEngine: the spectrum is on its own link, as on the box."""

    def __init__(self, x: np.ndarray) -> None:
        self.x = x
        self.t0 = time.monotonic()
        self.connected = False
        self.spectra = 0
        self.levels = 0
        self._last: tuple[Any, ...] | None = None

    async def connect(self, timeout: float | None = None) -> bool:
        self.connected = True
        return True

    async def close(self) -> None:
        self.connected = False

    async def command(self, name: str, timeout: float | None = None, **fields: Any) -> Any:
        if not self.connected:
            raise CdspUnavailable("fake engine down")
        if name == "GetCaptureSignalRmsSince":
            self.levels += 1
            return [-20.0] * 8
        if name != "GetSpectrum":
            raise CdspError(name, "Invalid", "fake: unknown")
        v = fields["value"]
        f0, f1, nb = float(v["min_freq"]), float(v["max_freq"]), int(v["n_bins"])
        n = 1 << max(0, math.ceil(math.log2(RATE / f0)))
        pos = int((time.monotonic() - self.t0) * RATE) % len(self.x)
        key = (pos // 480, f0, f1, nb)                    # every channel of one poll: one FFT
        if self._last is None or self._last[0] != key:
            sig = np.take(self.x, np.arange(pos - n, pos), mode="wrap")
            self._last = (key, engine_spectrum(sig, f0, f1, nb))
        self.spectra += 1
        fr, db = self._last[1]
        return {"frequencies": fr, "magnitudes": db}


@needs_browser
def test_v2_page_end_to_end_with_the_real_server(tmp_path: Path, monkeypatch: Any) -> None:
    """The real PhoneServer (S1) with the real Controller on the in-memory engine, in this
    process, and the production spectrum task (`open_spectrum`) on a fake engine playing pink
    noise; Chromium loads /v2/ from it: hello and state render, a stepper nudge and a chip hold
    reach the Controller, the worker installs, no console error under the real CSP. Then a
    second Chromium whose fake mic plays the same pink noise measures RESPONSE: real `pgm`
    windows, kept by the page, the coverage growing, the line flat (docs/14 §3.6)."""
    from k1000 import __main__ as main_mod

    wav = tmp_path / "pink.wav"
    engine = PlayingEngine(pink_wav(wav))
    monkeypatch.setattr(main_mod, "CamillaClient", lambda url, **kw: engine)

    async def go() -> tuple[Any, ...]:
        ctl, _ = make_ctl(tmp_path)
        await boot(ctl)
        await ctl.poll_once()
        ctl.rta = main_mod.open_spectrum(ctl, "ws://fake-engine")
        assert ctl.rta is not None
        srv = PhoneServer(ctl, settings(tmp_path), tick_s=0.02)
        await srv.start()
        try:
            port = srv.ports["plain"]
            m0 = ctl.state.master_db
            env = dict(os.environ, K1000_PLAYWRIGHT=PLAYWRIGHT or "")
            proc = await asyncio.create_subprocess_exec(
                NODE or "node", str(LAB / "real.mjs"), f"http://127.0.0.1:{port}/v2/", str(wav),
                cwd=str(BOX), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 170)
            await asyncio.sleep(0.2)
            return (m0, proc.returncode, out.decode(), err.decode(), ctl.state.master_db,
                    ctl.state.live_peq[2].on, dict(srv.rx2), dict(srv.stats), ctl.pgm_phones,
                    ctl.rta.pgm.seq)
        finally:
            await srv.stop()
            await ctl.rta.stop()
            ctl.rta = None
            await ctl.stop()

    m0, rc, out, err, master, on, rx, stats, pgm_phones, windows = run(go(), limit=220)
    lines = [ln for ln in out.splitlines() if ln.startswith("{")]
    assert lines, err[-3000:]
    res = json.loads(lines[-1])
    assert res["ok"] and rc == 0, _failed(res)
    assert master == pytest.approx(m0 - 0.5)          # the Controller moved, by one step
    assert on is False                                 # D-014 through the chip hold
    assert rx.get("nudge") == 1 and rx.get("band") == 1 and rx.get("hi", 0) >= 1 and rx.get("sub", 0) >= 3, rx
    # RESPONSE: the box closed windows and sent them; the page kept most of what it got
    assert engine.spectra > 0 and engine.levels > 0
    assert windows >= 16 and stats["pgm_sent"] >= 16, (windows, stats)
    assert res["resp"]["added"] >= 12 and res["resp"]["frames"] <= stats["pgm_sent"], (res["resp"], stats)
    assert pgm_phones == 0                             # left MEASURE, then gone: nobody measures
