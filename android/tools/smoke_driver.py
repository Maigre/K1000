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

    def start(self, leaf):
        self.stop()
        self.proc = subprocess.Popen([sys.executable, "-I", os.path.join(os.path.dirname(__file__), "smoke_server.py"),
                                      "--cert", f"{self.work}/{leaf}.pem", "--key", f"{self.work}/{leaf}.key",
                                      "--static", self.static, "--log", self.log],
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
    got = wait(lambda: [r for r in server.requests()[mark:] if r["path"] == "/"], 40)
    log = wait(lambda: "page loaded: https://10.42.0.1/" in logcat() and logcat(), 30) or logcat()
    ua = got[0]["ua"] if got else ""
    result("page https://10.42.0.1/ loaded in the WebView", bool(got) and "page loaded: https://10.42.0.1/" in log, ua[-60:])
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
    got = wait(lambda: [r for r in server.requests()[mark:] if r["path"] == "/"], 30)
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
        adb("shell", "am", "force-stop", rel)
        adb("uninstall", rel, check=False)

    server.stop()
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
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
