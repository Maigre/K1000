#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""The emulator smoke test of the K1000 app (run by tools/smoke.sh inside the emulator container).

It drives the SMOKE build (a throwaway, name-constrained test CA bundled instead of the K1000 CA;
the fallback mode first, following the emulator's own Wi-Fi once the "subnet" command points the
K1000-subnet filter at it, then the app's own join) against tools/smoke_server.py, which
stands in for the box at https://10.42.0.1/ (an iptables DNAT inside the emulator sends
10.42.0.1:443 to the server). It reads the app's log, the server's request log, the screen
(uiautomator) and the page itself (WebView remote debugging), and prints one line per check.

    smoke_driver.py --apk app-smoke.apk --work <dir with certs> --static <PWA static> --api 33

App 0.2: the app opens /v2/ first. There the server serves tools/bridge_test.html (the page side of
android/BRIDGE.md), and the driver checks the bridge through it: the handshake, haptics, immersive,
exclusion rects, keep-awake, diagnostics, refusals, Back (page overlays first, then the app's menu,
then hide), the page choice (set_ui, remembered, launcher shortcut), and a box without /v2/ (404:
v1 for the session). The v1 checks of 0.1 then run on the classic page.

The 0.2 review's fixes have their checks here too: a set_ui for the page already shown reloads
nothing; the shortcuts' actions count only through the non-exported alias; the app's menu closes
5 s after it was last touched itself, whatever the page is doing; the Edge test shows the bars;
and a hold on the page (the band chip's, the notch's long-press) reaches the page whole.

App 0.2.1 takes the K1000 Wi-Fi from the box (GET /api/wifi, docs/09-android.md §2): with the phone
on K1000 joined in Settings (as the camera's Wi-Fi QR code does), a fresh app on its Connect screen
reads the box's answer over the pinned TLS, stores it and joins K1000 itself; relaunched, it joins
alone. Also: an older box (404) keeps today's Connect screen, a look-alike box (another CA) is never
read, and the release APK (the real K1000 CA) takes nothing from the smoke box.

The 0.2.1 review's fixes (2026-10-13) have their checks too: a connection reset inside TLS is no
answer, asked again (not "not the K1000 certificate"); after Cancel in Android's dialog, the
Connect screen does not claim the app is on K1000 and the fallback box gets the page; a Connect
screen reopened while a slow box answers asks again by itself; a relaunch with the K1000 the camera
saved still in Settings; Android 17: the local network permission refused on the Connect screen,
then allowed from its line.
"""
import argparse
import asyncio
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import websockets

PKG = "gr.trentesept.k1000.smoke"
SSID = "K1000"                  # the emulated access point (smoke.sh: netsim --wifi-ssid/--wifi-password)
PSK = "abcd-efgh-jkmn"          # a test value in the box's format, nothing real
ACT = PKG + "/gr.trentesept.k1000.MainActivity"
ALIAS = PKG + "/gr.trentesept.k1000.Shortcut"   # the launcher shortcuts' non-exported way in (adb is root)
RESULTS = []


def sh(*cmd, check=True, timeout=120):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {r.returncode}: {r.stderr.strip()} {r.stdout.strip()}")
    return r.stdout


def adb(*args, check=True, timeout=120):
    return sh("adb", *args, check=check, timeout=timeout)


def ashell(cmd, check=True, timeout=120):
    return adb("shell", cmd, check=check, timeout=timeout)


def result(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def info(name, detail=""):
    print(f"INFO  {name}" + (f"  [{detail}]" if detail else ""), flush=True)


def wait(pred, timeout, step=1.0):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


class Server:
    def __init__(self, work, static, log):
        self.work, self.static, self.log, self.proc = work, static, log, None

    def start(self, leaf, v2="test", wifi="none", reset=0, slow=0, cut=0):
        self.stop()
        self.proc = subprocess.Popen([sys.executable, "-I", os.path.join(os.path.dirname(__file__), "smoke_server.py"),
                                      "--cert", f"{self.work}/{leaf}.pem", "--key", f"{self.work}/{leaf}.key",
                                      "--static", self.static, "--log", self.log, "--v2", v2, "--wifi", wifi,
                                      "--wifi-reset", str(reset), "--wifi-slow", str(slow), "--tls-cut", str(cut)],
                                     stderr=subprocess.DEVNULL)
        time.sleep(1.0)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None

    def requests(self):
        try:
            with open(self.log) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def mark(self):
        return len(self.requests())


def logcat():
    return adb("logcat", "-d", "-s", "K1000:*", "chromium:*", "cr_*:*", check=False)


def screen_xml():
    ashell("uiautomator dump /sdcard/ui.xml >/dev/null 2>&1", check=False, timeout=60)
    return ashell("cat /sdcard/ui.xml", check=False)


def screen_text():
    txt = " | ".join(t for t in re.findall(r'text="([^"]*)"', screen_xml()) if t)
    if "isn't responding" in txt and "Wait" in txt:
        # A slow emulator's system app ANR dialog (not ours) on top: dismiss it and look again.
        tap_text("Wait")
        time.sleep(2)
        txt = " | ".join(t for t in re.findall(r'text="([^"]*)"', screen_xml()) if t)
    return txt


def tap_text(text):
    """Taps the on-screen element whose text or hint is `text` (any case), or whose resource id
    is `text`; False when there is none."""
    for node in re.findall(r"<node [^>]*>", screen_xml()):
        names = [v.lower() for v in re.findall(r'(?:text|hint)="([^"]*)"', node)]
        rid = re.search(r'resource-id="[^"]*:id/([^"]*)"', node)
        if text.lower() in names or (rid and rid.group(1) == text):
            m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                ashell(f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
                return True
    return False


def node_bounds(rid):
    """The on-screen bounds (x1, y1, x2, y2) of the view with resource id `rid`, or None."""
    for node in re.findall(r"<node [^>]*>", screen_xml()):
        if re.search(rf'resource-id="[^"]*:id/{re.escape(rid)}"', node):
            m = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
            if m:
                return tuple(map(int, m.groups()))
    return None


def wifi_connected():
    return f'Wifi is connected to "{SSID}"' in ashell("cmd wifi status", check=False)


def wifi_stable(seconds=12):
    """Connected for `seconds` in a row (the emulated access point can drop a fresh client once)."""
    end = time.time() + 120
    while time.time() < end:
        t0 = time.time()
        while wifi_connected() and time.time() - t0 < seconds:
            time.sleep(2)
        if time.time() - t0 >= seconds:
            return True
        time.sleep(2)
    return False


def forget_saved_networks():
    for line in ashell("cmd wifi list-networks", check=False).splitlines()[1:]:
        nid = line.split()[0] if line.split() else ""
        if nid.isdigit():
            ashell(f"cmd wifi forget-network {nid}", check=False)


def qr(payload):
    """As if the app had scanned `payload` (the smoke build's "qr" command)."""
    ashell(f"am start -n {ACT} --es smoke qr --es payload '{payload}'", check=False)
    time.sleep(1.5)


def app_pid():
    return ashell(f"pidof {PKG}", check=False).strip().split(" ")[0]


def smoke(cmd, **extras):
    args = ["shell", "am", "start", "-n", ACT, "--es", "smoke", cmd]
    for k, v in extras.items():
        args += ["--es", k, v]
    adb(*args)
    time.sleep(1.5)


def wifi_subnet():
    """The emulator's Wi-Fi subnet ("10.0.2.0/24"): not the K1000 one, on purpose. "" while the
    Wi-Fi has no IPv4 address (the emulated access point drops a client now and then)."""
    out = ashell("ip -4 -o addr show", check=False)
    for m in re.finditer(r"^\d+:\s+(wlan\S*)\s+inet (\d+\.\d+\.\d+\.\d+/\d+)", out, re.M):
        return str(ipaddress.ip_interface(m.group(2)).network)
    for line in ashell("dumpsys connectivity", check=False, timeout=60).splitlines():
        if "InterfaceName: wlan" in line:
            for a in re.findall(r"(\d+\.\d+\.\d+\.\d+/\d+)", line.split("LinkAddresses:", 1)[-1].split("]", 1)[0]):
                return str(ipaddress.ip_interface(a).network)
    return ""


def start_app(cidr):
    """Starts the smoke app with fallback mode's subnet filter pointed at `cidr` (smoke build only)."""
    adb("shell", "am", "start", "-W", "-n", ACT, "--es", "smoke", "subnet", "--es", "cidr", cidr)


def start_fresh(cidr):
    """A real first run of the smoke app: the Connect screen (not the smoke build's fallback-mode
    preset), with the K1000 subnet pointed at the emulator's Wi-Fi `cidr`."""
    adb("shell", "am", "start", "-W", "-n", ACT, "--es", "smoke", "subnet", "--es", "cidr", cidr,
        "--ez", "smoke_fresh", "true")


def stored(pkg):
    """The app's stored Wi-Fi (shared_prefs/k1000.xml, adb is root): mode, ssid, wifi_from, and
    whether the password is the test one. The password itself is never printed."""
    xml = ashell(f"cat /data/data/{pkg}/shared_prefs/k1000.xml", check=False)
    get = lambda k: (re.search(rf'<string name="{k}">([^<]*)</string>', xml) or [None, None])[1]
    return {"mode": get("mode"), "ssid": get("ssid"), "wifi_from": get("wifi_from"),
            "psk_is_the_box_one": get("psk") == PSK}


def fresh_app():
    """The smoke app with no data, its permissions granted again."""
    adb("shell", "am", "force-stop", PKG)
    ashell(f"pm clear {PKG}", check=False)
    for p in ("android.permission.RECORD_AUDIO", "android.permission.CAMERA"):
        ashell(f"pm grant {PKG} {p}", check=False)


class Page:
    """Chrome DevTools Protocol on the app's WebView (debug builds only)."""

    def __init__(self):
        self.ws = None
        self.n = 0

    async def open(self):
        pid = wait(app_pid, 20)
        sock = f"webview_devtools_remote_{pid}"
        wait(lambda: sock in ashell("cat /proc/net/unix", check=False), 30)
        adb("forward", "tcp:9222", f"localabstract:{sock}")
        pages = []
        for _ in range(30):
            try:
                pages = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=5))
                pages = [p for p in pages if p.get("type") == "page" and "webSocketDebuggerUrl" in p]
                if pages:
                    break
            except Exception:
                pass
            await asyncio.sleep(1)
        if not pages:
            raise RuntimeError("no debuggable page in the WebView")
        self.ws = await websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=None)

    async def eval(self, expr, gesture=False, timeout=20):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": "Runtime.evaluate", "params": {
            "expression": expr, "awaitPromise": True, "returnByValue": True, "userGesture": gesture}}))
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), max(1.0, end - time.time())))
            except (asyncio.TimeoutError, TimeoutError):
                return "timeout"
            if msg.get("id") == self.n:
                r = msg.get("result", {})
                if "exceptionDetails" in r:
                    return "exception: " + json.dumps(r["exceptionDetails"].get("exception", {}).get("description", r["exceptionDetails"]))[:200]
                return r.get("result", {}).get("value")
        return "timeout"

    async def close(self):
        if self.ws:
            await self.ws.close()
        adb("forward", "--remove", "tcp:9222", check=False)


def window_dump():
    """Our activity window's dumpsys block (flags, insets), "" when it is not there."""
    out = ashell("dumpsys window windows", check=False, timeout=60)
    i = out.find(f"{PKG}/gr.trentesept.k1000.MainActivity}}:")
    if i < 0:
        return ""
    j = out.find("Window #", i)
    return out[i:j if j > 0 else i + 4000]


async def page_json(page, expr, timeout=10):
    """A JSON value the page computes (JSON.stringify'd there), or None."""
    end = time.time() + timeout
    while time.time() < end:
        v = await page.eval(f"JSON.stringify({expr})")
        if isinstance(v, str) and v not in ("null", "timeout") and not v.startswith("exception"):
            return json.loads(v)
        await asyncio.sleep(0.5)
    return None


async def diag(page):
    await page.eval("__k.msgs = __k.msgs.filter(m => m.t !== 'diag'); __send({t: 'diag'})")
    return await page_json(page, "__last('diag')") or {}


async def hold_checks(page):
    await page.eval("__send({t: 'fullscreen', on: true}); __send({t: 'exclusion', rects: []})")
    time.sleep(1.5)
    web = node_bounds("web") or (0, 0, 0, 0)
    d = await diag(page)
    dens = float(d.get("density") or 2.75)
    rects = await page_json(page, "Object.fromEntries([...document.querySelectorAll('#holds button')]"
                                  ".map(b => { const r = b.getBoundingClientRect(); return [b.id, [r.left, r.top, r.width, r.height]]; }))")
    if not rects:
        result("holds on the page: the test page's hold targets", False, "no #holds in the page")
        return

    async def hold(rid, dy_css=0, ms=900):
        await page.eval("__k.holds = []")
        r = rects[rid]
        x, y = int(web[0] + (r[0] + r[2] / 2) * dens), int(web[1] + (r[1] + r[3] / 2) * dens)
        ashell(f"input swipe {x} {y} {x} {y + int(dy_css * dens)} {ms}")
        await asyncio.sleep(0.6)
        return await page_json(page, "__k.holds[0] || null", timeout=5) or {}

    still = [await hold("hold-none"), await hold("hold-pany")]
    result("a still 0.9 s hold on the page reaches it whole (pointerup after 0.8 s or more, no pointercancel, "
           "no context menu): the app's long-press suppression eats no hold",
           all(h.get("end") == "pointerup" and h.get("ms", 0) >= 800 and not h.get("ctx") for h in still), json.dumps(still))
    await page.eval("document.body.classList.add('tall')")
    drift = []
    for dy in (4, 8, 11, 16):
        h = await hold("hold-pany", dy)
        drift.append(f"{dy} px: {h.get('end')} at {h.get('ms')} ms (moved {h.get('moved')})")
        await page.eval("scrollTo(0, 0)")
    hn = await hold("hold-none", 16)
    await page.eval("document.body.classList.remove('tall'); scrollTo(0, 0)")
    info("a 0.9 s hold drifting down on a page that scrolls, touch-action pan-y (v1's band chips)",
         "; ".join(drift) + f" | touch-action none, 16 px: {hn.get('end')} at {hn.get('ms')} ms")


async def edge_test_check(page):
    """Diagnostics › Edge test (review 4): the bars show for the test (immersive hides Back from
    every edge swipe, so with the bars hidden the test would pass with broken bands too), a swipe
    inside a band is never Back, one outside is counted, and the page's full screen comes back."""
    await page.eval("__send({t: 'fullscreen', on: true}); __send({t: 'exclusion', rects: []})")
    time.sleep(2)
    h_full = await page.eval("innerHeight")
    adb("logcat", "-c")
    smoke("edgetest")
    time.sleep(2.5)
    h_test = await page.eval("innerHeight")
    size = re.search(r"(\d+)x(\d+)", ashell("wm size", check=False))
    hgt = int(size.group(2)) if size else 2340
    d = await diag(page)
    dens = float(d.get("density") or 2.75)
    y_in, y_out = hgt // 2, hgt // 2 + int(200 * dens)   # the bands: 200 dp tall, centred
    counts = []
    for y in (y_in, y_out):
        adb("logcat", "-c")
        ashell(f"input swipe 3 {y} 500 {y} 250")
        time.sleep(1.5)
        counts.append(len(re.findall(r"edge test: Back fired", logcat())))
    adb("logcat", "-c")
    smoke("edgeend")
    time.sleep(2.5)
    h_after = await page.eval("innerHeight")
    ended = "edge test: ended" in logcat()
    smoke("hidepanels")
    result("Diagnostics › Edge test shows the bars for its 20 s: inside a band no Back, outside one is "
           "counted; the page's full screen comes back after (review 4)",
           isinstance(h_full, int) and isinstance(h_test, int) and h_test < h_full and counts[0] == 0
           and counts[1] >= 1 and h_after == h_full and ended,
           f"innerHeight {h_full} -> {h_test} -> {h_after}; Backs inside {counts[0]}, outside {counts[1]}")


async def bridge_checks(page, server, api, cidr):
    """The page bridge (android/BRIDGE.md) against tools/bridge_test.html at /v2/."""
    href = await page.eval("location.href")
    result("CDP attached to the v2 page", href == "https://10.42.0.1/v2/", str(href))
    nat = await page_json(page, "__k.natives.slice(-1)[0] || null", timeout=15) or {}
    caps = set(nat.get("caps", []))
    result("bridge: handshake {t:'native', v:1, caps, ui:'v2'} with a port, to the box's page",
           nat.get("t") == "native" and nat.get("v") == 1 and nat.get("ui") == "v2"
           and {"haptic", "fullscreen", "exclusion", "keep_awake", "diag", "back", "set_ui"} <= caps
           and str(nat.get("app", "")).startswith("0.2."), json.dumps(nat)[:200])
    ready = wait(lambda: "bridge: ready v=1 caps=[back]" in logcat(), 15)
    result("bridge: the page's {t:'ready'} is heard by the app", bool(ready))

    # A set_ui for the page already on screen (a page that re-sends its choice at each ready, a
    # shortcut to the page in use): nothing reloads (review 1).
    adb("logcat", "-c")
    m = server.mark()
    for _ in range(3):
        await page.eval("__send({t: 'set_ui', ui: 'v2'})")
        await asyncio.sleep(0.3)
    time.sleep(3)
    again = [r for r in server.requests()[m:] if r["path"] == "/v2/"]
    noop = logcat().count("chosen: already shown, nothing to do")
    result("set_ui v2 three times on the v2 page: nothing reloads (review 1)",
           not again and noop == 3, f"{len(again)} reloads, {noop} no-ops logged")

    # Haptics: every kind accepted, the detent rate capped (25/s).
    adb("logcat", "-c")
    kinds = ["tick", "click", "confirm", "reject", "long_press", "confirm2", "reject3"]
    for k in kinds:
        await page.eval(f"__send({{t: 'haptic', kind: '{k}'}})")
        await asyncio.sleep(0.4)
    time.sleep(1)
    got = re.findall(r"bridge: haptic (\w+) played=(true|false)", logcat())
    result("bridge: every haptic kind accepted (performHapticFeedback)", [g[0] for g in got] == kinds, str(got))
    await page.eval("for (let i = 0; i < 12; i++) __send({t: 'haptic', kind: 'tick'})")
    time.sleep(1)
    d = await diag(page)
    m = re.search(r"(\d+) dropped", str(d.get("haptics_count", "")))
    result("bridge: ticks above 25/s are dropped, never queued", bool(m) and int(m.group(1)) >= 9,
           str(d.get("haptics_count")))

    # Diagnostics.
    want = ["webview_version", "sdk", "model", "unprocessed_supported", "mics", "haptic_primitives", "haptic_effects",
            "system_haptics", "gesture_insets", "exclusion_rects"]
    result("bridge: {t:'diag'} answers the bench questions (WebView, SDK, model, UNPROCESSED, mics, haptics)",
           all(k in d for k in want) and d.get("sdk") == api and d.get("webview_version"),
           ", ".join(f"{k}={json.dumps(d.get(k))[:40]}" for k in ("webview_version", "sdk", "model",
                                                               "unprocessed_supported", "mics", "haptic_primitives")))
    info("diag (emulator)", json.dumps({k: d.get(k) for k in ("haptic_effects", "has_vibrator", "gesture_insets",
                                                              "density", "viewport_css")})[:300])

    # Refusals.
    await page.eval("__k.errs = []; __send('{\"t\":\"eval\",\"js\":\"x\"}'); __send({t: 'haptic', kind: 'buzz'});"
                    " __send('not json'); __send({t: 'exclusion', rects: [[0, 0, -5, 10]]})")
    errs = await page_json(page, "__k.errs.length >= 4 ? __k.errs : null") or []
    result("bridge: unknown types and bad fields are refused whole, answered {t:'err'}",
           [e.get("m") for e in errs] == ["eval", "haptic", "", "exclusion"], json.dumps(errs)[:200])

    # Immersive: the page grows when the bars go; {t:'insets'} once the page takes the cutout.
    await page.eval("__send({t: 'fullscreen', on: false})")
    time.sleep(2)
    h_off = await page.eval("innerHeight")
    await page.eval("__send({t: 'fullscreen', on: true, cutout: true})")
    time.sleep(2)
    h_on = await page.eval("innerHeight")
    ins = await page_json(page, "__last('insets')")
    result("bridge: fullscreen on = immersive (the page gains the bars' height), insets sent",
           isinstance(h_on, int) and isinstance(h_off, int) and h_on > h_off and isinstance(ins, dict),
           f"innerHeight {h_off} -> {h_on}; insets {ins}")
    await page.eval("__send({t: 'fullscreen', on: true})")

    # Exclusion rects: CSS px in, view pixels applied, read back in CSS px.
    rects = [[0, 300, 40, 200], [352, 300, 40, 200]]
    await page.eval(f"__send({{t: 'exclusion', rects: {json.dumps(rects)}}})")
    time.sleep(1)
    d = await diag(page)
    result("bridge: exclusion rects applied to the WebView (CSS px -> px -> CSS px)", d.get("exclusion_rects") == rects,
           f"read back {d.get('exclusion_rects')}, density {d.get('density')}")

    # Keep awake: the app's own window flag follows the page. (Chromium may keep the screen on
    # by itself through the WebView's own keepScreenOn, which the app does not fight: reported.)
    await page.eval("__send({t: 'keep_awake', on: false})")
    time.sleep(1)
    d_off = await diag(page)
    off = window_dump()
    await page.eval("__send({t: 'keep_awake', on: true})")
    time.sleep(1)
    d_on = await diag(page)
    fl = lambda w: (re.search(r"\bfl=[^\n]*", w) or [""])[0][:60]
    result("bridge: keep_awake off/on clears and sets the app's FLAG_KEEP_SCREEN_ON",
           d_off.get("window_keep_screen_on") is False and d_on.get("window_keep_screen_on") is True,
           f"off: {d_off.get('window_keep_screen_on')}, on: {d_on.get('window_keep_screen_on')}")
    info("keep_awake off: who still keeps the screen on", f"views {d_off.get('keep_screen_on_views')}; "
         f"window as Android sees it: {fl(off)}")

    # Holds on the page (a band chip's IN/OUT, the graph's long-press notch): the app suppresses
    # WebView's own long-press (no selection, no context menu); a hold must still reach the page
    # whole. Then, as evidence for the page side: a hold that drifts down on a pan-y target (v1's
    # band chips) on a page that scrolls, against the same drift on a touch-action none target.
    await hold_checks(page)

    # Back: a page overlay first; then the app's menu; Back again hides (never finishes).
    await page.eval("__k.overlays = 1")
    adb("logcat", "-c")
    ashell("input keyevent KEYCODE_BACK")
    time.sleep(1.5)
    log = logcat()
    result("Back with a page overlay open: the page closes it (back_handled), no menu, K1000 stays",
           "back: the page closed an overlay" in log and "the app menu" not in log
           and await page.eval("__k.overlays") == 0)
    adb("logcat", "-c")
    ashell("input keyevent KEYCODE_BACK")
    time.sleep(0.8)
    txt = screen_text()
    log = logcat()
    result("Back at the page's top level: the app's menu ('Back again now: hide K1000'), MUTE row free",
           "back: the page is at its top level" in log and "Back again now: hide K1000" in txt, txt[:100])
    # Thomas works the page with the menu up: taps on the page (not on the menu) every 0.8 s must
    # not keep it open (review 3): it closes 5 s after it was last touched itself.
    t0 = time.time()
    while time.time() - t0 < 7:
        ashell("input tap 540 1500")   # the page, well below the menu, above the footer
        time.sleep(0.8)
    txt = screen_text()
    result("the app's menu closes by itself, even while the page under it is being touched (review 3)",
           "Back again now" not in txt, txt[:80])
    smoke("hidepanels")
    await page.eval("__k.deaf = true")
    adb("logcat", "-c")
    pid = app_pid()
    m = server.mark()
    ashell("input keyevent KEYCODE_BACK")
    time.sleep(0.7)
    ashell("input keyevent KEYCODE_BACK")
    time.sleep(2)
    log = logcat()
    resumed = ashell("dumpsys activity activities | grep -E -m2 'mResumedActivity|topResumedActivity'", check=False)
    result("a page that does not answer Back: the app's menu after 0.4 s; Back again within 2 s hides K1000",
           "did not answer back" in log and "K1000 hidden (moveTaskToBack)" in log and PKG + "/" not in resumed
           and app_pid() == pid, re.sub(r"\s+", " ", resumed)[:120])
    adb("shell", "am", "start", "-W", "-n", ACT)
    time.sleep(2)
    href = await page.eval("location.href")
    reloaded = [r for r in server.requests()[m:] if r["path"] == "/v2/"]
    result("reopened: the same page, not reloaded, not restarted", href == "https://10.42.0.1/v2/" and not reloaded
           and app_pid() == pid, str(href))
    await page.eval("__k.deaf = false")

    # Back gestures and the exclusion rects (gesture navigation): a swipe in from the left edge
    # inside an excluded rect must never be Back. Outside it: recorded (immersive may first show
    # the bars instead). Needs an AVD with a navigation bar (smoke.sh: hw.mainKeys=no).
    nav = ashell("cmd overlay enable-exclusive --category com.android.internal.systemui.navbar.gestural", check=False)
    time.sleep(5)
    await page.eval(f"__send({{t: 'exclusion', rects: {json.dumps([[0, 300, 40, 200]])}}})")

    async def swipes(label):
        time.sleep(1.5)
        d = await diag(page)
        dens = float(d.get("density") or 2.75)
        g = d.get("gesture_insets") or {}
        inside, outside = int(400 * dens), int(700 * dens)
        fired = []
        for y in (inside, outside):
            adb("logcat", "-c")
            ashell(f"input swipe 3 {y} 500 {y} 250")
            time.sleep(1.5)
            log = logcat()
            fired.append("back: " in log or "did not answer" in log)
            if fired[-1]:
                time.sleep(2.5)      # let a menu the swipe opened age past "Back again"
                smoke("hidepanels")
        return g, fired, d.get("exclusion_rects")

    await page.eval("__send({t: 'fullscreen', on: true})")
    g, (in_imm, out_imm), ex = await swipes("immersive")
    await page.eval("__send({t: 'fullscreen', on: false})")
    g2, (in_bars, out_bars), _ = await swipes("bars")
    await page.eval("__send({t: 'fullscreen', on: true})")
    gestural = float(g.get("left", 0) or 0) > 0 or float(g2.get("left", 0) or 0) > 0
    detail = (f"immersive: inside Back {in_imm}, outside Back {out_imm}; bars shown: inside {in_bars}, outside "
              f"{out_bars}; gesture insets {g2}; rects {ex}")
    if gestural:
        result("gesture nav: a swipe in from the edge inside the page's exclusion rect is never Back",
               not in_imm and not in_bars and out_bars, detail)
        await edge_test_check(page)
    else:
        info("gesture nav not available on this AVD (no navigation bar): edge swipes not tested", detail + f"; {nav.strip()[:60]}")
    ashell("cmd overlay enable-exclusive --category com.android.internal.systemui.navbar.threebutton", check=False)
    time.sleep(4)
    ashell("input keyevent KEYCODE_BACK")   # close a menu the outside swipe may have opened
    time.sleep(0.5)
    smoke("hidepanels")
    await page.eval("__send({t: 'exclusion', rects: []})")


def dismiss_request_dialog():
    """Closes a Wi-Fi request dialog an app left behind (it would sit over the next checks)."""
    for _ in range(3):
        txt = screen_text().split(" | ")
        if "OK" in txt and any("Something came up" in t for t in txt):
            tap_text("OK")
        elif "Cancel" in txt:
            tap_text("Cancel")
        else:
            return
        time.sleep(1.5)


def wifi_framework_log(n=40):
    """The Wi-Fi framework's own lines (network requests, the connect dialog), for a failed check."""
    keep = re.compile(r"WifiNetworkFactory|NetworkRequestDialog|NetworkRequestUi|ActiveModeWarden|"
                      r"WifiClientModeImpl|ClientModeImpl|WifiConnectivityManager|WifiScan")
    return [ln[-200:] for ln in adb("logcat", "-d", check=False).splitlines() if keep.search(ln)][-n:]


def open_connect_screen():
    """Back (the bridge test page is at its top level: the app's menu) › hold "K1000 Wi-Fi"."""
    ashell("input keyevent KEYCODE_BACK")
    time.sleep(1.5)
    b = node_bounds("menu_wifi")
    if b:
        x, y = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        ashell(f"input swipe {x} {y} {x} {y} 1200")   # a hold
    return bool(b)


async def box_wifi_checks(server, api, cidr):
    """The K1000 Wi-Fi taken from the box (app 0.2.1). The phone is on K1000 joined in Settings, as
    the camera's Wi-Fi QR code leaves it; the app starts fresh (its Connect screen)."""
    ashell("cmd wifi set-verbose-logging enabled", check=False)
    def first_run(leaf, wifi, reset=0, cut=0, final=False, local=True):
        """A fresh app on its Connect screen, the box served with `leaf` and `wifi` (its first `cut`
        TLS handshakes and `reset` /api/wifi requests reset): (the logged "box wifi:" verdict match or None — the
        first one, or with `final` the first that is not RETRY —, the requests the box saw, the
        screen, the log). `local` False (Android 17): the local network permission not granted."""
        fresh_app()
        if api >= 37:
            ashell(f"pm {'grant' if local else 'revoke'} {PKG} android.permission.ACCESS_LOCAL_NETWORK", check=False)
        server.start(leaf, wifi=wifi, reset=reset, cut=cut)
        if not wifi_connected():
            ashell(f"cmd wifi connect-network {SSID} wpa2 {PSK}", check=False)
        wifi_stable(8)
        adb("logcat", "-c")
        m = server.mark()
        start_fresh(wait(wifi_subnet, 30, step=3) or cidr)
        if not local:
            return None, [], "", ""
        verdicts = "ADOPT|SAME_UNJOINED|SAME|ABSENT|REFUSED|INVALID" + ("" if final else "|RETRY")
        got = wait(lambda: re.search(rf"box wifi: ({verdicts})\b[^\n]*", logcat()), 40)
        time.sleep(2)
        return got, server.requests()[m:], screen_text(), logcat()

    got, reqs, txt, log = first_run("good", "none")
    asked = [r for r in reqs if r["path"] == "/api/wifi"]
    result("an older box (404 at /api/wifi): the Connect screen stays as today (scan, type, or the fallback), "
           "nothing stored, no join",
           bool(got) and got.group(1) == "ABSENT" and len(asked) == 1 and "Connect to K1000" in txt
           and "older K1000" in txt and "requesting the K1000 Wi-Fi" not in log and stored(PKG)["mode"] is None,
           f"{got.group(0) if got else 'no verdict'}; screen: {txt[:60]}")

    got, reqs, txt, log = first_run("wrongca", f"{SSID}:{PSK}", final=True)
    for ln in re.findall(r"box wifi: (?:TLS failure at|TLS refused|\S+ did not answer)[^\n]*", log):
        info("look-alike box: the app's TLS error", ln[:400])
    result("a box whose certificate is not from the K1000 CA (a look-alike at 10.42.0.1): /api/wifi is never "
           "read, nothing taken, the Connect screen stays",
           bool(got) and got.group(1) == "REFUSED" and not [r for r in reqs if r["path"] == "/api/wifi"]
           and "Connect to K1000" in txt and "requesting the K1000 Wi-Fi" not in log and stored(PKG)["mode"] is None,
           f"{got.group(0)[:90] if got else 'no verdict'}")

    # Review 2026-10-13 (1): the real box, its first connection closed in the middle of the TLS
    # handshake (Android: SSLHandshakeException "connection closed", no certificate in it), the
    # next request reset once read (the edge of range, the box's service restarting). That is no
    # answer, asked again 2 then 4 s later, then taken: never "not the K1000 certificate", which
    # used to be final for any SSLException.
    ashell(f"cmd wifi network-requests-remove-user-approved-access-points {PKG}", check=False)
    got, reqs, txt, log = first_run("good", f"{SSID}:{PSK}", cut=1, reset=1, final=True)
    asked = [r for r in reqs if r["path"] == "/api/wifi"]
    cuts = [r for r in reqs if r["path"] == "(tls cut)"]
    why = re.findall(r"box wifi: (\S+) did not answer \(([^)]*)\)", log)
    ssl_seen = sorted({w[:70] for h, w in why if "javax.net.ssl." in w})
    result("review 1: a connection closed inside TLS (mid-handshake), then one reset once the request is in: no answer, "
           "asked again, then the Wi-Fi is taken: never 'not the K1000 certificate'",
           bool(got) and got.group(1) == "ADOPT" and bool(ssl_seen) and "TLS refused" not in log
           and "box wifi: REFUSED" not in log and bool(cuts) and len(asked) >= 2 and stored(PKG)["mode"] == "JOIN",
           f"{got.group(0)[:20] if got else 'no verdict'}; {len(cuts)} cut, {len(asked)} /api/wifi; "
           f"{len(re.findall(r'box wifi: RETRY', log))} RETRY; SSLException seen as no answer: {ssl_seen}")
    for h, w in why:
        info("review 1: no answer from", f"{h}: {w[:150]}")

    # Review 2026-10-13 (3): Android's dialog then lists no K1000 (the emulator never does while the
    # Settings network holds it, §8; a phone might: "No devices found") › Cancel › Change K1000
    # Wi-Fi. The box gives the same Wi-Fi, but the app is not on it: the Connect screen must not say
    # it "already joins" it, and must point to the fallback box. Then that documented way out: tick
    # it, Join K1000: the app follows the network joined in Settings, the page loads.
    dialog = wait(lambda: "Cancel" in screen_text().split(" | ") and screen_text(), 30) or ""
    tap_text("Cancel")
    nc = wait(lambda: "Not connected to K1000" in screen_text() and screen_text(), 20) or ""
    adb("logcat", "-c")
    tap_text("Change K1000 Wi-Fi")
    same = wait(lambda: re.search(r"box wifi: [A-Z_]+[^\n]*", logcat()), 30)
    time.sleep(1.5)
    txt = screen_text()
    result("review 3: after Cancel in Android's dialog, Change K1000 Wi-Fi: the same Wi-Fi from the box, but the "
           "app is not on it: the screen says so and points to the fallback box (not 'already joins'), no re-join",
           bool(same) and same.group(0).startswith("box wifi: SAME_UNJOINED") and "the app is not on it" in txt
           and "already joins" not in txt and "requesting the K1000 Wi-Fi" not in logcat(),
           f"dialog: {dialog[:40]!r}; status: {'Not connected' if nc else nc[:40]!r}; "
           f"{same.group(0)[:40] if same else 'not asked'}; screen: {txt[:60]}")
    tap_text("setup_system")
    time.sleep(0.5)
    tap_text("Join K1000")
    up = wait(lambda: "K1000 link up (SYSTEM)" in logcat(), 40)
    page = wait(lambda: "page loaded: https://10.42.0.1/v2/" in logcat(), 30)
    st = stored(PKG)
    result("…the way out the docs give: tick 'I joined K1000 in Android's Wi-Fi settings', Join K1000: the app "
           "follows the network the camera joined (fallback mode), the page loads",
           bool(up) and bool(page) and st["mode"] == "SYSTEM", f"stored {st['mode']}; link up {bool(up)}; "
           f"page {bool(page)}")
    adb("shell", "am", "force-stop", PKG)
    dismiss_request_dialog()

    # The real thing, on a phone that never approved the app's own join (Android's dialog shows once).
    ashell(f"cmd wifi network-requests-remove-user-approved-access-points {PKG}", check=False)
    got, reqs, txt, log = first_run("good", f"{SSID}:{PSK}")
    asked = [r for r in reqs if r["path"] == "/api/wifi"]
    taken = "box wifi: K1000 Wi-Fi taken from the box (K1000" in log
    st = stored(PKG)
    result("phone on K1000 (joined in Settings), app fresh: it takes the K1000 Wi-Fi from the box over the "
           "pinned TLS ('K1000 Wi-Fi taken from the box'), stored where a scan stores it, JOIN mode",
           bool(got) and got.group(1) == "ADOPT" and len(asked) == 1 and taken
           and st == {"mode": "JOIN", "ssid": SSID, "wifi_from": "box smoke-1, band bg", "psk_is_the_box_one": True}
           and f"requesting the K1000 Wi-Fi ({SSID})" in log,
           f"{len(asked)} /api/wifi; stored {st}; screen: {txt[:80]}")
    shown = bool(re.search(r"status: Joining K1000… · K1000 Wi-Fi taken from the box", log))
    # The emulator's radio reports no scan results while it is associated (WifiNetworkFactory:
    # "Received 0 scan results" with K1000 connected), so Android's dialog cannot find K1000 while
    # the Settings network holds it; a real phone scans while connected. So, once the app has asked
    # (its request stays open), the Settings network is dropped, and the dialog's next scan sees
    # K1000. On the Pixel 4a: docs/09-android.md §10, checklist item 1.
    searching = "Searching for device" in screen_text()
    forget_saved_networks()
    dialog = wait(lambda: "Connect" in screen_text().split(" | ") and screen_text(), 40) or ""
    tapped = tap_text("Connect") if dialog else False
    up = wait(lambda: "K1000 link up (JOIN)" in logcat(), 40)
    page = wait(lambda: "page loaded: https://10.42.0.1/v2/" in logcat(), 30)
    result("…then it joins K1000 itself (WifiNetworkSpecifier, Android's dialog once): link up in JOIN mode, "
           "the page loads",
           bool(up) and bool(page) and shown, f"dialog: searching first {searching}, K1000 listed {bool(dialog)}, "
           f"Connect tapped {tapped}; the joining screen under it says 'K1000 Wi-Fi taken from the box': {shown}")
    if not up:
        for ln in wifi_framework_log():
            info("wifi framework", ln)
    adb("shell", "am", "force-stop", PKG)
    time.sleep(3)
    adb("logcat", "-c")
    m = server.mark()
    t0 = time.time()
    adb("shell", "am", "start", "-W", "-n", ACT)
    up = wait(lambda: "K1000 link up (JOIN)" in logcat(), 40)
    page = wait(lambda: "page loaded: https://10.42.0.1/v2/" in logcat(), 30)
    txt = screen_text()
    again = [r for r in server.requests()[m:] if r["path"] == "/api/wifi"]
    result("relaunched: it joins K1000 by itself in JOIN mode (no dialog, no scan, /api/wifi not asked again)",
           bool(up) and bool(page) and "Connect to device" not in txt and "Connect to K1000" not in txt and not again,
           f"{time.time() - t0:.1f} s; screen: {txt[:60]}")
    if not up:
        for ln in wifi_framework_log():
            info("wifi framework", ln)

    # The Connect screen opened while the app is on K1000 itself (Back › menu › hold K1000 Wi-Fi):
    # the box is asked and gives the same Wi-Fi, so nothing changes (no re-join, the page stays).
    if up:
        adb("logcat", "-c")
        b = open_connect_screen()
        same = wait(lambda: re.search(r"box wifi: [A-Z_]+[^\n]*", logcat()), 30)
        time.sleep(2)
        txt = screen_text()
        log = logcat()
        result("the Connect screen opened while the app is on K1000: the box gives the same Wi-Fi, nothing "
               "changes (no re-join)",
               bool(same) and same.group(0).startswith("box wifi: SAME ") and "already joins" in txt
               and "requesting the K1000 Wi-Fi" not in log and "K1000 link lost" not in log,
               f"{same.group(0)[:60] if same else 'not asked'}; menu found: {b}; screen: {txt[:70]}")
        tap_text("Cancel")
        time.sleep(1)

        # Review 2026-10-13 (2): a slow box (its answer takes about 10 s). Connect screen, Cancel
        # while the box is still answering, Connect screen again: once the first request ends, its
        # answer is dropped and the reopened screen asks by itself (it used to wait for a Wi-Fi
        # change), its own try 1.
        server.start("good", wifi=f"{SSID}:{PSK}", slow=10)
        adb("logcat", "-c")
        b1 = open_connect_screen()
        asking = wait(lambda: "box wifi: asking the box" in logcat(), 15)
        time.sleep(1)
        tap_text("Cancel")
        time.sleep(1)
        b2 = open_connect_screen()
        t0 = time.time()
        same = wait(lambda: re.search(r"box wifi: SAME [^\n]*", logcat()), 45)
        time.sleep(1.5)
        txt = screen_text()
        log = logcat()
        result("review 2: Cancel and reopen the Connect screen while a slow box answers: the old answer is dropped, "
               "and the reopened screen asks again by itself (try 1), then 'already joins'",
               bool(asking) and bool(same) and "an answer to an earlier Connect screen, dropped" in log
               and len(re.findall(r"box wifi: asking the box over network \S+ \(try 1\)", log)) >= 2
               and "already joins" in txt,
               f"menu {b1}/{b2}; {same.group(0)[:30] + f' {time.time() - t0:.0f} s after reopening' if same else 'never asked again'}; "
               f"screen: {txt[:50]}")
        tap_text("Cancel")
        time.sleep(1)
        server.start("good", wifi=f"{SSID}:{PSK}")

    # Review 2026-10-13 (4): relaunched in the state the docs leave the phone in, the K1000 the
    # camera saved still in Android's Wi-Fi settings and connected (above, it had been forgotten so
    # that the emulator's dialog could list K1000). The app must still join by itself, no dialog.
    adb("shell", "am", "force-stop", PKG)
    time.sleep(3)
    for _ in range(3):
        ashell(f"cmd wifi connect-network {SSID} wpa2 {PSK}", check=False)
        if wait(wifi_connected, 30, step=2):
            break
    settings_on = wifi_stable(8)
    if not settings_on:
        info("review 4: Android's Wi-Fi", " / ".join(ashell("cmd wifi status", check=False).splitlines()[:4]))
        info("review 4: saved networks", " / ".join(ashell("cmd wifi list-networks", check=False).splitlines()[:4]))
    adb("logcat", "-c")
    m = server.mark()
    t0 = time.time()
    adb("shell", "am", "start", "-W", "-n", ACT)
    up = wait(lambda: "K1000 link up (JOIN)" in logcat(), 45)
    page = wait(lambda: "page loaded: https://10.42.0.1/v2/" in logcat(), 30)
    txt = screen_text()
    again = [r for r in server.requests()[m:] if r["path"] == "/api/wifi"]
    result("review 4: relaunched with the K1000 the camera saved still in Settings (connected): it joins K1000 "
           "by itself in JOIN mode (no dialog, no scan, /api/wifi not asked)",
           bool(up) and bool(page) and "Connect to device" not in txt and "Connect to K1000" not in txt
           and not again and settings_on,
           f"{time.time() - t0:.1f} s; Settings network connected first: {settings_on}; screen: {txt[:70]}")
    if not up:
        for ln in wifi_framework_log():
            info("wifi framework", ln)
    adb("shell", "am", "force-stop", PKG)
    dismiss_request_dialog()

    if api >= 37:
        # Review 2026-10-13 (5): Android 17, the local network permission refused on the Connect
        # screen (first run, on K1000): no prompt loop, the screen's line says so, and a tap on it
        # asks again (the screen has no Retry; the box used to be never asked again in that run).
        m = server.mark()
        first_run("good", f"{SSID}:{PSK}", local=False)
        prompt = wait(lambda: "Allow" in screen_text().split(" | ") and screen_text(), 20) or ""
        refused = tap_text("Don’t allow") or tap_text("Don't allow")
        line = wait(lambda: "Tap here to allow it" in screen_text(), 10)
        time.sleep(3)
        txt = screen_text()
        looped = "Allow" in txt.split(" | ")
        n0 = len([r for r in server.requests()[m:] if r["path"] == "/api/wifi"])
        tapped = tap_text("setup_box")
        prompt2 = wait(lambda: "Allow" in screen_text().split(" | ") and screen_text(), 15) or ""
        tap_text("Allow")
        got = wait(lambda: re.search(r"box wifi: (ADOPT|SAME_UNJOINED|SAME|ABSENT|REFUSED|INVALID)\b[^\n]*",
                                     logcat()), 40)
        result("review 5 (api37): the local network permission refused on the Connect screen: no prompt loop, the "
               "screen says so; a tap on that line asks again › Allow: the box is asked, the Wi-Fi taken",
               bool(prompt) and refused and not looped and line and n0 == 0 and tapped and bool(prompt2)
               and bool(got) and got.group(1) == "ADOPT",
               f"prompt {bool(prompt)}, refused {refused}, loop {looped}, line {line}, prompt again {bool(prompt2)}, "
               f"/api/wifi before allowed {n0}, {got.group(0)[:30] if got else 'no verdict'}")
        if not (line and got):
            info("review 5: screen after the refusal", txt[:200])
            for ln in re.findall(r"(?:box wifi|K1000 link|status)[^\n]*", logcat())[-12:]:
                info("review 5: app log", ln[:200])
        adb("shell", "am", "force-stop", PKG)
        dismiss_request_dialog()


async def run(a):
    server = Server(a.work, a.static, f"{a.work}/requests.jsonl")
    api = int(a.api)

    # --- the emulator: root, and 10.42.0.1:443 -> the smoke server on the container's 8443
    adb("root", check=False)
    adb("wait-for-device")
    time.sleep(2)
    ashell("iptables -t nat -D OUTPUT -p tcp -d 10.42.0.1 --dport 443 -j DNAT --to-destination 10.0.2.2:8443", check=False)
    ashell("iptables -t nat -I OUTPUT -p tcp -d 10.42.0.1 --dport 443 -j DNAT --to-destination 10.0.2.2:8443")
    ashell("svc wifi enable", check=False)
    # Phases 0-4 run the app's fallback mode (it follows the Wi-Fi joined in Android's settings, on
    # the K1000 subnet only): Android joins the emulated K1000 here, as Thomas would in Settings.
    ashell(f"cmd wifi connect-network {SSID} wpa2 {PSK}", check=False)
    result("Android joined the emulated WPA2 'K1000' access point (fallback-mode setup)", wifi_stable())
    server.start("good")

    # Android's one-time "Viewing full screen" hint (the app is immersive from 0.2) would cover
    # the screen the checks read: mark it seen, as a user's first "Got it" does.
    ashell("settings put secure immersive_mode_confirmations confirmed", check=False)
    adb("install", "-r", a.apk, timeout=240)
    ashell(f"pm clear {PKG}", check=False)
    grants = ["android.permission.RECORD_AUDIO", "android.permission.CAMERA"]
    for p in grants:
        ashell(f"pm grant {PKG} {p}", check=False)

    # --- Android 17: the local network permission gates the page
    if api >= 37:
        ashell(f"pm revoke {PKG} android.permission.ACCESS_LOCAL_NETWORK", check=False)
        adb("logcat", "-c")
        mark = server.mark()
        adb("shell", "am", "start", "-W", "-n", ACT)
        time.sleep(6)
        txt = screen_text()
        asked = "Allow local network access" in txt or "local network" in txt.lower() or "nearby" in txt.lower()
        result("api37: local network permission is asked before the page loads", asked and server.mark() == mark,
               txt[:160])
        adb("shell", "am", "force-stop", PKG)
        ashell(f"pm grant {PKG} android.permission.ACCESS_LOCAL_NETWORK")

    if a.only == "boxwifi":
        # Only the K1000 Wi-Fi taken from the box (tools/smoke.sh with K1000_SMOKE_ONLY=boxwifi).
        wifi_stable(6)
        await box_wifi_checks(server, api, wait(wifi_subnet, 60, step=3))
        server.stop()
        return summary(api)

    # --- 0. fallback mode follows K1000 only: the emulator's Wi-Fi is not on 10.42.0.0/24, so the
    # release logic must ignore it (no binding, no page) until the smoke "subnet" command says so.
    wifi_stable(6)
    cidr = wait(wifi_subnet, 60, step=3)
    if not cidr:
        info("no IPv4 address on the emulator's Wi-Fi", ashell("ip -4 -o addr show", check=False)[:300])
        raise RuntimeError("cannot find the emulator's Wi-Fi subnet")
    adb("logcat", "-c")
    mark = server.mark()
    adb("shell", "am", "start", "-W", "-n", ACT)
    time.sleep(8)
    log = logcat()
    txt = screen_text()
    result("fallback mode ignores a Wi-Fi that is not on 10.42.0.0/24 (no binding, no page, 'Waiting for K1000')",
           "ignoring Wi-Fi network" in log and "process bound" not in log and "K1000 link up" not in log
           and server.mark() == mark and "Waiting for K1000" in txt, f"emulator Wi-Fi {cidr}; screen: {txt[:70]}")

    # --- 1. the page loads over TLS from the bundled CA, through the network security config
    adb("logcat", "-c")
    smoke("subnet", cidr=cidr)
    got = wait(lambda: [r for r in server.requests()[mark:] if r["path"] == "/v2/"], 40)
    log = wait(lambda: "page loaded: https://10.42.0.1/v2/" in logcat() and logcat(), 30) or logcat()
    ua = got[0]["ua"] if got else ""
    result("page https://10.42.0.1/v2/ (v2, app 0.2's default) loaded in the WebView",
           bool(got) and "page loaded: https://10.42.0.1/v2/" in log, ua[-60:])
    result("user agent carries K1000-Android/<version>", "K1000-Android/" in ua)
    result("TLS accepted by Android itself (network security config; onReceivedSslError never ran)",
           bool(got) and "TLS:" not in log)
    result("process bound to the Wi-Fi network", "process bound to network" in log and ": true" in log)

    smoke("trustcheck")
    result("K1000Trust passes the served leaf on the device's own crypto",
           "smoke: trustcheck PASS" in logcat())

    page = Page()
    await page.open()
    try:
        await bridge_checks(page, server, api, cidr)
        # The classic page from here on (the 0.1 checks): THIS PHONE's escape, from the page.
        adb("logcat", "-c")
        m = server.mark()
        await page.eval("__send({t: 'set_ui', ui: 'v1'})")
        got = wait(lambda: [r for r in server.requests()[m:] if r["path"] == "/"], 20)
        loaded = wait(lambda: re.search(r"page loaded: https://10\.42\.0\.1/$", logcat(), re.M), 20)
        result("set_ui v1 from the page: the app opens the classic page https://10.42.0.1/",
               bool(got) and bool(loaded) and "page v1 chosen" in logcat())
        await page.close()
        page = Page()
        await page.open()
        href = await page.eval("location.href")
        result("CDP attached to the page", href == "https://10.42.0.1/", str(href))
        result("secure context", await page.eval("window.isSecureContext") is True)
        ctl = None
        for _ in range(30):
            ctl = await page.eval("navigator.serviceWorker.ready.then(r => !!r.active && !!navigator.serviceWorker.controller)", timeout=10)
            if ctl is True:
                break
            await asyncio.sleep(1)
        result("service worker registered and controlling the page", ctl is True, str(ctl))
        shell_sw = [r for r in server.requests()[mark:] if r["path"].startswith("/sw.js")]
        result("service worker script fetched over the same TLS", bool(shell_sw))
        if ctl is not True or not shell_sw:
            # What the page itself says (a failed registration's own error), and the log.
            regs = await page.eval("navigator.serviceWorker.getRegistrations().then(r => 'registrations: ' + r.length)", timeout=10)
            again = await page.eval("navigator.serviceWorker.register('/sw.js', {scope: '/'}).then(r => 'register ok, active: ' + !!r.active)"
                                    ".catch(e => 'register failed: ' + e.name + ': ' + e.message)", timeout=20)
            info("service worker diagnostics", f"{regs}; {again}; /sw.js requests now: "
                 f"{len([r for r in server.requests()[mark:] if r['path'].startswith('/sw.js')])}")
            for ln in [ln for ln in logcat().splitlines() if re.search(r"console:|[Ss]ervice ?[Ww]orker|sw\.js|ERR_|TLS", ln)][-12:]:
                info("log", ln[-180:])
        result("navigator.vibrate() accepted (VIBRATE permission)", await page.eval("navigator.vibrate(40)", gesture=True) is True)
        mic = await page.eval("navigator.mediaDevices.getUserMedia({audio:true}).then(s => {const n = s.getAudioTracks().length; s.getTracks().forEach(t => t.stop()); return 'audio tracks: ' + n}).catch(e => 'refused: ' + e.name)")
        result("microphone granted to the box's page (RECORD_AUDIO plumbing)", mic == "audio tracks: 1", str(mic))
        cam = await page.eval("navigator.mediaDevices.getUserMedia({video:true}).then(() => 'granted').catch(e => 'refused: ' + e.name)")
        result("camera refused to the page", str(cam).startswith("refused"), str(cam))
        img = await page.eval("new Promise(r => {const i = new Image(); i.onload = () => r('loaded'); i.onerror = () => r('blocked'); i.src = 'https://www.google.com/favicon.ico?' + Date.now(); setTimeout(() => r('timeout'), 8000)})")
        result("a foreign host is blocked inside the WebView", img == "blocked", str(img))
        # WebSockets bypass shouldInterceptRequest: TLS must stop them (no CA is trusted for any
        # other host), and the refusal must not disturb the box's page. The smoke server by its
        # other address: it would log the request if TLS let it through.
        adb("logcat", "-c")
        m = server.mark()
        ws = await page.eval("new Promise(r => {const w = new WebSocket('wss://10.0.2.2:8443/ws?foreign'); "
                             "w.onopen = () => r('open'); w.onerror = () => r('error'); setTimeout(() => r('timeout'), 10000)})", timeout=15)
        time.sleep(2)
        txt = screen_text()
        through = [r for r in server.requests()[m:] if "foreign" in r["path"]]
        log = logcat()
        result("a WebSocket to another host is refused at TLS, and the page stays (no 'Not the K1000 box')",
               ws != "open" and not through and "Not the K1000 box" not in txt and await page.eval("location.href") == "https://10.42.0.1/",
               f"ws {ws}; onReceivedSslError: {'TLS: refused a connection to another host' in log}")
        adb("logcat", "-c")
        ws = await page.eval("new Promise(r => {const w = new WebSocket('wss://www.google.com/'); "
                             "w.onopen = () => r('open'); w.onerror = () => r('error'); setTimeout(() => r('timeout'), 10000)})", timeout=15)
        time.sleep(2)
        lines = [ln.split("console: ", 1)[-1][:110] for ln in logcat().splitlines() if "WebSocket" in ln or "TLS: refused" in ln]
        info("a WebSocket to a public host (base config: no CA trusted)", f"ws {ws}; " + " / ".join(lines)[:220])
        probe = await page.eval("fetch('/probe?' + Date.now(), {cache: 'no-store'}).then(r => r.status).catch(e => 'error: ' + e)")
        result("network fetch to the box over the bound Wi-Fi", probe == 200, str(probe))

        # --- 2. WebView traffic follows bindProcessToNetwork.
        # netd marks every socket with its network; a socket of a process bound with
        # bindProcessToNetwork also carries the "explicitly selected" bit (0x10000). For this
        # check only marked traffic reaches the server, and unmarked traffic to 10.42.0.1 is
        # rejected (the server closes every connection, so nothing is reused across the switch).
        fetch = "fetch('/probe?' + Date.now(), {cache: 'no-store'}).then(r => r.status).catch(e => 'error: ' + e)"
        nat = "-p tcp -d 10.42.0.1 --dport 443"
        ashell(f"iptables -t nat -D OUTPUT {nat} -j DNAT --to-destination 10.0.2.2:8443")
        ashell(f"iptables -t nat -I OUTPUT {nat} -m mark --mark 0x10000/0x10000 -j DNAT --to-destination 10.0.2.2:8443")
        ashell(f"iptables -I OUTPUT {nat} -j REJECT --reject-with tcp-reset")

        async def through():
            m = server.mark()
            status = await page.eval(fetch)
            return status, any(r["path"].startswith("/probe") for r in server.requests()[m:])

        def counters():
            nat_rules = ashell("iptables -t nat -L OUTPUT -v -n -x", check=False).splitlines()[2:3]
            rej = ashell("iptables -L OUTPUT -v -n -x", check=False).splitlines()[2:3]
            return "mark-DNAT pkts " + (nat_rules[0].split()[0] if nat_rules else "?") + \
                ", REJECT pkts " + (rej[0].split()[0] if rej else "?")

        try:
            for attempt in range(3):
                wifi_stable(6)
                adb("logcat", "-c")
                bound, hit_bound = await through()
                smoke("unbind")
                unbound, hit_unbound = await through()
                smoke("rebind")
                rebound, hit_rebound = await through()
                flapped = "K1000 link lost" in logcat()
                if bound == 200 and rebound == 200 and not flapped:
                    break
                # The emulated Wi-Fi sometimes drops or stalls a moment; a pass on any attempt is
                # conclusive (only an explicitly bound socket can carry the mark).
                info(f"binding check attempt {attempt + 1} inconclusive",
                     f"bound {bound}, unbound {unbound}, rebound {rebound}, Wi-Fi dropped {flapped}, {counters()}")
                if flapped:
                    wait(lambda: "K1000 link up" in logcat(), 60)
                time.sleep(5)
            result("the page's own requests carry the process binding (bound: through; unbound: not; rebound: through)",
                   bound == 200 and hit_bound and unbound != 200 and not hit_unbound and rebound == 200 and hit_rebound,
                   f"bound {bound}, unbound {unbound}, rebound {rebound}")
        finally:
            ashell(f"iptables -D OUTPUT {nat} -j REJECT --reject-with tcp-reset", check=False)
            ashell(f"iptables -t nat -D OUTPUT {nat} -m mark --mark 0x10000/0x10000 -j DNAT --to-destination 10.0.2.2:8443", check=False)
            ashell(f"iptables -t nat -I OUTPUT {nat} -j DNAT --to-destination 10.0.2.2:8443")

        adb("logcat", "-c")
        ashell("svc wifi disable")
        lost = wait(lambda: "K1000 link lost" in logcat(), 30)
        time.sleep(3)
        txt = screen_text()
        result("Wi-Fi gone: the app's 'Not connected to K1000' view", bool(lost) and "The phone left the K1000 Wi-Fi" in txt, txt[:120])
        m = server.mark()
        t0 = time.time()
        probe = await page.eval(fetch)
        hits = [r for r in server.requests()[m:] if r["path"].startswith("/probe")]
        result("binding kept on the lost Wi-Fi: the page's requests fail at once, nothing leaks elsewhere",
               probe != 200 and not hits and time.time() - t0 < 5, f"fetch -> {probe} in {time.time() - t0:.1f} s")
        ashell("svc wifi enable")
        up = wait(lambda: "K1000 link up" in logcat(), 60)
        time.sleep(3)
        txt = screen_text()
        result("Wi-Fi back: link up again, rebound, the app's view gone",
               bool(up) and "The phone left the K1000 Wi-Fi" not in txt and "RETRY" not in txt.upper().split(" | "), txt[:120])
        probe = await page.eval(fetch)
        result("page reaches the box again", probe == 200, str(probe))

        # --- 2b. Android refuses the binding (as under a VPN that apps may not bypass): the app must
        # not call the link up, must say so, and must take the binding by itself once allowed.
        smoke("refusebind")
        adb("logcat", "-c")
        ashell("svc wifi disable")
        wait(lambda: "K1000 link lost" in logcat(), 30)
        ashell("svc wifi enable")
        blocked = wait(lambda: "refused to bind this app" in logcat(), 60)
        time.sleep(2)
        txt = screen_text()
        m = server.mark()
        probe = await page.eval(fetch)
        hits = [r for r in server.requests()[m:] if r["path"].startswith("/probe")]
        result("binding refused (VPN): 'K1000 blocked on this phone', link not up, nothing reaches the box",
               bool(blocked) and "K1000 blocked on this phone" in txt and "K1000 link up" not in logcat()
               and probe != 200 and not hits, f"fetch -> {probe}; screen: {txt[:60]}")
        adb("logcat", "-c")
        smoke("allowbind")
        up = wait(lambda: "binding allowed again" in logcat(), 20)
        time.sleep(2)
        txt = screen_text()
        probe = await page.eval(fetch)
        result("binding allowed again: taken by itself within seconds, the view gone, the page reaches the box",
               bool(up) and "K1000 blocked" not in txt and probe == 200, f"fetch -> {probe}")
    finally:
        await page.close()

    # --- 2c. the page choice is remembered; a launcher shortcut switches back to v2
    adb("shell", "am", "force-stop", PKG)
    wifi_stable(6)
    adb("logcat", "-c")
    start_app(cidr)
    v1 = wait(lambda: re.search(r"page loaded: https://10\.42\.0\.1/$", logcat(), re.M), 40)
    result("the choice is remembered: restarted, the app opens v1 again", bool(v1))
    # The shortcuts' actions count only through the non-exported alias (review 1): the same
    # action sent to the exported MainActivity, as any installed app could, changes nothing.
    adb("logcat", "-c")
    m = server.mark()
    adb("shell", "am", "start", "-W", "-a", "gr.trentesept.k1000.action.UI_V2", "-n", ACT)
    time.sleep(4)
    log = logcat()
    loads = [r for r in server.requests()[m:] if r["path"] in ("/", "/v2/")]
    result("a shortcut's action sent to the exported MainActivity (as any app could): ignored, no reload, "
           "the choice kept (review 1)",
           "not from K1000's own launcher shortcut" in log and "page v2 chosen" not in log and not loads,
           f"{len(loads)} page loads")
    adb("logcat", "-c")
    adb("shell", "am", "start", "-W", "-a", "gr.trentesept.k1000.action.UI_V2", "-n", ALIAS)
    v2 = wait(lambda: "page loaded: https://10.42.0.1/v2/" in logcat(), 30)
    dump = ashell(f"dumpsys shortcut | grep -A80 'Package: {PKG}'", check=False)
    sc = re.findall(r"id=(ui_v1|ui_v2|diag)", dump)
    result("launcher shortcut 'New page (v2)' through the alias: back on v2 (the three dynamic shortcuts exist)",
           bool(v2) and {"ui_v1", "ui_v2", "diag"} <= set(sc), " ".join(sorted(set(sc))))
    aimed = re.findall(r"cmp=[^ }]*", dump)
    info("the dynamic shortcuts' targets (dumpsys shortcut)", " ".join(sorted(set(aimed)))[:200] or "not printed")
    ready = wait(lambda: "bridge: ready" in logcat(), 10)
    info("the v2 page's content after the shortcut (v1's service worker installed by now)",
         "the bridge page answered ready" if ready else
         "no bridge ready: v1's sw.js answered /v2/ from its cached '/' (box-side, android/BRIDGE.md)")
    adb("logcat", "-c")
    m = server.mark()
    adb("shell", "am", "start", "-W", "-a", "gr.trentesept.k1000.action.UI_V2", "-n", ALIAS)
    time.sleep(4)
    again = [r for r in server.requests()[m:] if r["path"] in ("/", "/v2/")]
    result("the shortcut 'New page (v2)' again, v2 on screen: nothing reloads (review 1)",
           "already shown, nothing to do" in logcat() and not again, f"{len(again)} page loads")

    # --- 3. certificates the app must refuse (fresh app data: no service-worker cache)
    for leaf, expect, label in (("wrongca", "not signed by the K1000 CA", "a look-alike CA (same name, other key)"),
                                ("otherip", "does not name 10.42.0.1", "a test-CA leaf for another address")):
        adb("shell", "am", "force-stop", PKG)
        ashell(f"pm clear {PKG}", check=False)
        for p in grants:
            ashell(f"pm grant {PKG} {p}", check=False)
        if api >= 37:
            ashell(f"pm grant {PKG} android.permission.ACCESS_LOCAL_NETWORK", check=False)
        server.start(leaf)
        for attempt in range(3):
            wifi_stable(10)
            adb("logcat", "-c")
            start_app(cidr)
            refused = wait(lambda: "TLS: refused" in logcat() and logcat(), 40) or ""
            time.sleep(2)
            txt = screen_text()
            if refused or "K1000 link lost" not in logcat():
                break
            info(f"refusal check '{leaf}' attempt {attempt + 1}: the emulated Wi-Fi dropped, once more")
            adb("shell", "am", "force-stop", PKG)
        result(f"refused: {label}", expect in refused and "Not the K1000 box" in txt,
               (re.search(r"TLS: refused[^\n]*", refused) or [""])[0][:100] + " / screen: " + txt[-90:])

    # --- 4. what Android's own validator does with the CA's name constraints (documented gap)
    adb("shell", "am", "force-stop", PKG)
    ashell(f"pm clear {PKG}", check=False)
    if api >= 37:
        ashell(f"pm grant {PKG} android.permission.ACCESS_LOCAL_NETWORK", check=False)
    server.start("extradns")
    adb("logcat", "-c")
    mark = server.mark()
    start_app(cidr)
    time.sleep(8)
    log = logcat()
    loaded = "page loaded: https://10.42.0.1/" in log and "TLS: refused" not in log
    smoke("trustcheck")
    explicit = "trustcheck FAIL" in logcat() and "evil.example is outside" in logcat()
    info("a test-CA leaf that also names evil.example (outside the CA's constraints), for 10.42.0.1",
         f"Android's own validator accepted it: {loaded}; K1000Trust refuses it: {explicit}")

    # --- 4b. a box without the v2 page (404 at /v2/, today's box): v1 for this session
    adb("shell", "am", "force-stop", PKG)
    ashell(f"pm clear {PKG}", check=False)
    for p in grants:
        ashell(f"pm grant {PKG} {p}", check=False)
    if api >= 37:
        ashell(f"pm grant {PKG} android.permission.ACCESS_LOCAL_NETWORK", check=False)
    server.start("good", v2="none")
    wifi_stable(6)
    adb("logcat", "-c")
    mark = server.mark()
    start_app(cidr)
    v1 = wait(lambda: re.search(r"page loaded: https://10\.42\.0\.1/$", logcat(), re.M), 40)
    paths = [r["path"] for r in server.requests()[mark:]]
    result("a box without /v2/ (404): the app shows the classic page for the session",
           "/v2/" in paths and "/" in paths and bool(v1) and "no /v2/ on this box" in logcat(), str(paths[:6]))

    # --- 4c. the K1000 Wi-Fi taken from the box (app 0.2.1)
    await box_wifi_checks(server, api, cidr)

    # --- 5. the app's own join: WifiNetworkSpecifier, Android's dialog, remembered approval
    adb("shell", "am", "force-stop", PKG)
    forget_saved_networks()
    ashell(f"cmd wifi network-requests-remove-user-approved-access-points {PKG}", check=False)
    ashell(f"pm clear {PKG}", check=False)
    for p in grants:
        ashell(f"pm grant {PKG} {p}", check=False)
    if api >= 37:
        ashell(f"pm grant {PKG} android.permission.ACCESS_LOCAL_NETWORK", check=False)
    server.start("good")
    wait(lambda: not wifi_connected(), 20)
    adb("logcat", "-c")
    adb("shell", "am", "start", "-W", "-n", ACT)
    time.sleep(3)
    qr("https://10.42.0.1/")
    result("a URL code is refused as a Wi-Fi code (on the device)",
           bool(wait(lambda: "qr refused: This is not a Wi-Fi QR code" in logcat(), 10)))
    mark = server.mark()
    qr(f"WIFI:T:WPA;S:{SSID};P:{PSK};;")
    dialog = wait(lambda: "Connect" in screen_text().split(" | ") and screen_text(), 30) or ""
    result("first join: Android's own connect dialog for K1000", "K1000" in dialog, dialog[:120])
    tapped = tap_text("Connect")
    up = wait(lambda: "K1000 link up (JOIN)" in logcat(), 40)
    got = wait(lambda: [r for r in server.requests()[mark:] if r["path"] in ("/", "/v2/")], 30)
    default_net = ashell("dumpsys connectivity | grep -m1 'Active default network'", check=False).strip()
    result("Connect: joined K1000, process bound, page loaded", tapped and bool(up) and bool(got),
           re.sub(r"\s+", " ", default_net))
    result("the K1000 network is not the phone's default network (mobile data would stay default)",
           "Active default network: none" in default_net, default_net)

    adb("logcat", "-c")
    ashell("svc wifi disable")
    wait(lambda: "K1000 link lost" in logcat(), 30)
    time.sleep(2)
    txt = screen_text()
    result("Wi-Fi off: 'Wi-Fi is off' view", "Wi-Fi is off" in txt, txt[-120:])
    ashell("svc wifi enable")
    up = wait(lambda: "K1000 link up (JOIN)" in logcat(), 60)
    txt = screen_text()
    result("Wi-Fi on: the app rejoins K1000 by itself, without a dialog",
           bool(up) and "Connect to device" not in txt and "The phone left" not in txt, txt[-100:])

    adb("shell", "am", "force-stop", PKG)
    released = wait(lambda: not wifi_connected(), 20)
    adb("logcat", "-c")
    t0 = time.time()
    adb("shell", "am", "start", "-W", "-n", ACT)
    up = wait(lambda: "K1000 link up (JOIN)" in logcat(), 40)
    result("app closed: Android leaves K1000; reopened: rejoined without a dialog",
           bool(released) and bool(up) and "Connect to device" not in screen_text(), f"{time.time() - t0:.1f} s")

    adb("shell", "am", "force-stop", PKG)
    ashell(f"cmd wifi network-requests-remove-user-approved-access-points {PKG}", check=False)
    adb("logcat", "-c")
    adb("shell", "am", "start", "-W", "-n", ACT)
    wait(lambda: "Cancel" in screen_text().split(" | "), 30)
    tap_text("Cancel")
    gone = wait(lambda: "link unavailable" in logcat(), 30)
    time.sleep(15)
    txt = screen_text()
    result("Cancel in the dialog: 'Not connected to K1000' with Retry, and no dialog loop",
           bool(gone) and "K1000 was not found, or the connection was declined" in txt and "Cancel" not in txt.split(" | "),
           txt[-120:])

    adb("shell", "am", "force-stop", PKG)

    # --- 6. the signed release APK itself (minified, the real K1000 CA): it starts, its scanner
    # opens, it joins K1000 with a typed password, and it refuses the smoke server's test-CA leaf.
    if a.release_apk:
        rel = "gr.trentesept.k1000"
        ract = rel + "/gr.trentesept.k1000.MainActivity"
        adb("install", "-r", a.release_apk, timeout=240)
        ashell(f"pm clear {rel}", check=False)
        ashell(f"pm grant {rel} android.permission.CAMERA", check=False)
        if api >= 37:
            ashell(f"pm grant {rel} android.permission.ACCESS_LOCAL_NETWORK", check=False)
        # First run on a Wi-Fi that is not K1000's (the emulator's is not 10.42.0.x; a home Wi-Fi
        # on a real phone), the smoke box serving /api/wifi: the release app does not ask (its
        # subnet filter is the real one), takes nothing, and stays on its Connect screen.
        server.start("good", wifi=f"{SSID}:{PSK}")
        ashell(f"cmd wifi connect-network {SSID} wpa2 {PSK}", check=False)
        wifi_stable(8)
        adb("logcat", "-c")
        m = server.mark()
        adb("shell", "am", "start", "-W", "-n", ract)
        time.sleep(8)
        log = logcat()
        txt = screen_text()
        result("release APK, first run on a Wi-Fi that is not on 10.42.0.0/24 (joined in Settings): the box is "
               "not asked, nothing taken, 'Connect to K1000'",
               "Connect to K1000" in txt and "box wifi:" not in log and "requesting the K1000 Wi-Fi" not in log
               and not [r for r in server.requests()[m:] if r["path"] == "/api/wifi"], f"emulator Wi-Fi {wifi_subnet()}")
        adb("shell", "am", "force-stop", rel)
        ashell(f"pm clear {rel}", check=False)
        ashell(f"pm grant {rel} android.permission.CAMERA", check=False)
        if api >= 37:
            ashell(f"pm grant {rel} android.permission.ACCESS_LOCAL_NETWORK", check=False)
        forget_saved_networks()
        ashell(f"cmd wifi network-requests-remove-user-approved-access-points {rel}", check=False)
        wait(lambda: not wifi_connected(), 20)
        adb("logcat", "-c")
        adb("shell", "am", "start", "-W", "-n", ract)
        time.sleep(3)
        txt = screen_text()
        result("release APK: starts on 'Connect to K1000'", "Connect to K1000" in txt, txt[:80])
        tap_text("Scan the Wi-Fi QR code")
        scanner = wait(lambda: "Scan the Wi-Fi code on the K1000 screen" in screen_text(), 20)
        result("release APK: the QR scanner opens (ZXing survives R8)", bool(scanner))
        ashell("input keyevent KEYCODE_BACK")
        time.sleep(2)
        tap_text("Wi-Fi password (e.g. abcd-efgh-jkmn)") or tap_text("setup_password")
        ashell(f"input text {PSK}")
        ashell("input keyevent KEYCODE_BACK")   # close the keyboard
        time.sleep(1)
        tap_text("Join K1000")
        wait(lambda: "Connect" in screen_text().split(" | "), 30)
        tap_text("Connect")
        up = wait(lambda: "K1000 link up (JOIN)" in logcat(), 40)
        refused = wait(lambda: "TLS: refused" in logcat() and logcat(), 40) or ""
        time.sleep(2)
        txt = screen_text()
        result("release APK: joins K1000 with the typed password, refuses a non-K1000 certificate",
               bool(up) and "not issued by the K1000 CA" in refused and "Not the K1000 box" in txt,
               (re.search(r"TLS: refused[^\n]*", refused) or [""])[0][:100])
        ashell("input keyevent KEYCODE_BACK")
        time.sleep(1.0)
        txt = screen_text()
        pid = ashell(f"pidof {rel}", check=False).strip()
        menu = "Back again now: hide K1000" in txt
        # Three quick Backs: the menu may have outlived its 2 s (Back then closes it, Back shows it
        # again, Back hides K1000); otherwise the first one hides K1000 and the rest go to the launcher.
        for _ in range(3):
            ashell("input keyevent KEYCODE_BACK")
        time.sleep(2)
        resumed = ashell("dumpsys activity activities | grep -E -m2 'mResumedActivity|topResumedActivity'", check=False)
        result("release APK: Back shows the app's menu, Back again hides K1000 (not finished)",
               menu and rel + "/" not in resumed and ashell(f"pidof {rel}", check=False).strip() == pid and pid != "",
               txt[:80])
        adb("shell", "am", "force-stop", rel)
        adb("uninstall", rel, check=False)

    server.stop()
    return summary(api)


def summary(api):
    ok = all(r[1] for r in RESULTS)
    print(f"\n{sum(r[1] for r in RESULTS)}/{len(RESULTS)} checks passed (API {api})", flush=True)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--static", required=True)
    ap.add_argument("--api", required=True)
    ap.add_argument("--release-apk", default="")
    ap.add_argument("--only", default="", help="boxwifi: only the K1000 Wi-Fi taken from the box")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
