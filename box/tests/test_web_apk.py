# SPDX-License-Identifier: GPL-3.0-or-later
"""The Android app offered by the box (docs/06-phone.md §4): the first-time page's "Get the K1000
app" button, `/k1000.apk` (portal and HTTPS), `/api/wifi` (HTTPS only, to a device on the AP
only), and install.sh's step 5b that puts one APK in /opt/k1000/apk.

The servers run in-process on 127.0.0.1 like test_web.py. A "device on the AP" is a client bound
to another loopback address (127.0.0.2) that the fake neighbour table lists on the AP interface.
install.sh's block is cut out of the script and run with fake install/stat/systemctl, the way
tools/tests/test_phone_net.py does it.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest

from k1000.web import server as web
from k1000.web.server import PhoneServer, PhoneSettings, load_apk, load_static, psk_id, psk_on_air
from test_web import _self_signed, free_port, run, settings

BOX = Path(__file__).resolve().parents[1]
INSTALL = BOX / "install.sh"
NET = BOX / "net"
PSK = "correct-horse-k1000"
ARP_HEAD = "IP address       HW type     Flags       HW address            Mask     Device\n"


def _apk_bytes(size: int = 64 * 1024, seed: str = "k1000") -> bytes:
    """A zip archive (what an APK is), `size` bytes of incompressible payload."""
    buf = io.BytesIO()
    payload = b"".join(hashlib.sha256(f"{seed}{i}".encode()).digest() for i in range(size // 32 + 1))
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("AndroidManifest.xml", b"<manifest/>")
        z.writestr("classes.dex", payload[:size])
    return buf.getvalue()


def _put_apk(folder: Path, version: str, size: int = 64 * 1024) -> bytes:
    folder.mkdir(parents=True, exist_ok=True)
    body = _apk_bytes(size, version)
    (folder / f"K1000-{version}.apk").write_bytes(body)
    return body


def _on_air(tmp: Path) -> None:
    """The AP (re)started with tmp/ap.psk: what the hostapd unit's ExecStartPost records."""
    (tmp / "on-air").write_text(psk_id((tmp / "ap.psk").stat()) + "\n")


def _new_psk(tmp: Path, psk: str) -> None:
    """setup-ap.sh --new-password: a new file moved in (a new inode), the AP not restarted yet."""
    (tmp / "ap.psk.tmp").write_text(psk + "\n")
    os.replace(tmp / "ap.psk.tmp", tmp / "ap.psk")


def _arp(path: Path, *rows: tuple[str, str]) -> None:
    path.write_text(ARP_HEAD + "".join(
        f"{ip:<17}0x1         0x2         aa:bb:cc:dd:ee:{i:02x}     *        {dev}\n"
        for i, (ip, dev) in enumerate(rows)))


# ======================================================================= load_apk
def test_load_apk_takes_the_newest_k1000_version_only(tmp_path: Path) -> None:
    d = tmp_path / "apk"
    assert load_apk(d) is None                                   # no folder: no app, no error
    d.mkdir()
    assert load_apk(d) is None
    _put_apk(d, "0.2.0")
    newest = _put_apk(d, "0.10.0")
    _put_apk(d, "0.9.7")
    (d / "app-bench.apk").write_bytes(_apk_bytes(1024))          # never offered: not a release name
    (d / "K1000-x.apk").write_bytes(_apk_bytes(1024))
    (d / "K1000-0.2.0.apk.sha256").write_text("x\n")
    apk = load_apk(d)
    assert apk is not None and apk.version == "0.10.0" and apk.body == newest
    assert apk.filename == "K1000-0.10.0.apk"
    assert apk.sha256 == hashlib.sha256(newest).hexdigest() and apk.etag == f'"{apk.sha256}"'


def test_load_apk_refuses_what_is_not_an_apk(tmp_path: Path, monkeypatch: Any) -> None:
    d = tmp_path / "apk"
    d.mkdir()
    (d / "K1000-0.2.0.apk").write_bytes(b"<html>not a zip</html>")
    assert load_apk(d) is None
    (d / "K1000-0.2.0.apk").write_bytes(_apk_bytes(4096))
    monkeypatch.setattr(web, "APK_MAX", 1024)
    assert load_apk(d) is None                                   # too large to hold in memory
    monkeypatch.setattr(web, "APK_MAX", 64 << 20)
    assert load_apk(d) is not None


def test_settings_apk_dir() -> None:
    assert PhoneSettings().apk_dir == Path("/opt/k1000/apk")
    assert PhoneSettings.from_settings({"phone": {"apk_dir": "/srv/apk"}}).apk_dir == Path("/srv/apk")
    assert PhoneSettings.from_settings({"phone": {"apk_dir": ""}}).apk_dir == Path("/opt/k1000/apk")


# ======================================================================= the portal (plain HTTP)
def _portal(tmp_path: Path, apk: bool = True, **kw: Any) -> PhoneServer:
    folder = tmp_path / "apk"
    if apk:
        _put_apk(folder, "0.2.0")
    cfg = settings(tmp_path, address="10.42.0.1", hosts=(), http_port=80, plain_app="off",
                   apk_dir=folder, **kw)
    srv = PhoneServer(object(), cfg, clock=lambda: 1000.0)       # frozen: buckets never refill
    srv._static, _ = load_static(web.STATIC_DIR)
    srv._apk = load_apk(folder)
    srv.ports["http"] = 80
    return srv


def _parse(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    h, _, body = raw.partition(b"\r\n\r\n")
    line, *hl = h.decode("latin-1").split("\r\n")
    return int(line.split()[1]), {k.strip().lower(): v.strip() for k, v in (x.split(":", 1) for x in hl)}, body


def _get(srv: PhoneServer, path: str, host: str = "10.42.0.1", method: str = "GET", peer: str = "10.42.0.23",
         extra: str = "") -> tuple[int, dict[str, str], bytes]:
    head = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n{extra}\r\n"
    return _parse(srv.portal_response(head.encode("latin-1"), peer=peer))


def test_portal_serves_the_apk_as_a_download(tmp_path: Path) -> None:
    srv = _portal(tmp_path)
    body = (tmp_path / "apk/K1000-0.2.0.apk").read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    for host in ("10.42.0.1", "k1000.home.arpa"):
        st, h, got = _get(srv, "/k1000.apk", host=host)
        assert st == 200 and got == body
        assert h["content-type"] == "application/vnd.android.package-archive"
        assert h["content-disposition"] == 'attachment; filename="K1000-0.2.0.apk"'
        assert int(h["content-length"]) == len(body)
        assert h["etag"] == f'"{sha}"'
    st, h, got = _get(srv, "/k1000.apk", method="HEAD")
    assert st == 200 and got == b"" and int(h["content-length"]) == len(body)
    st, h, got = _get(srv, "/k1000.apk", extra=f'If-None-Match: "{sha}"\r\n')
    assert st == 304 and got == b"" and h["etag"] == f'"{sha}"'
    # another name (portal on: every name resolves to the box) is a connectivity probe
    st, h, _ = _get(srv, "/k1000.apk", host="connectivitycheck.gstatic.com")
    assert st == 302


def test_portal_apk_404_without_an_apk_and_rate_limited_per_device(tmp_path: Path) -> None:
    srv = _portal(tmp_path, apk=False)
    st, h, body = _get(srv, "/k1000.apk")
    assert st == 404 and b"no K1000 app" in body
    srv = _portal(tmp_path)
    burst = int(web.HTTP_LIMITS["apk"][1])
    for _ in range(burst):
        assert _get(srv, "/k1000.apk", peer="10.42.0.23")[0] == 200
    st, h, _ = _get(srv, "/k1000.apk", peer="10.42.0.23")
    assert st == 429 and "retry-after" in h
    assert _get(srv, "/k1000.apk", peer="10.42.0.23", method="HEAD")[0] == 200   # HEAD costs nothing
    assert _get(srv, "/k1000.apk", peer="10.42.0.24")[0] == 200                  # per device
    assert srv.stats["apk_sent"] == burst + 1 and srv.stats["http_limited"] == 1


def test_portal_never_serves_the_wifi_settings(tmp_path: Path) -> None:
    srv = _portal(tmp_path)
    (tmp_path / "ap.psk").write_text(PSK + "\n")
    for host in ("10.42.0.1", "k1000.home.arpa", "connectivitycheck.gstatic.com"):
        st, _, body = _get(srv, "/api/wifi", host=host)
        assert st == 404 and PSK.encode() not in body, host


def test_first_time_page_offers_the_app_only_when_there_is_one(tmp_path: Path) -> None:
    srv = _portal(tmp_path)
    sha = hashlib.sha256((tmp_path / "apk/K1000-0.2.0.apk").read_bytes()).hexdigest()
    st, _, body = _get(srv, "/")
    text = body.decode()
    assert st == 200 and "{{" not in text and "<!--APK-->" in text
    assert 'href="/k1000.apk"' in text and 'download="K1000-0.2.0.apk"' in text
    assert "Get the K1000 app" in text and "version 0.2.0" in text and f"sha256 {sha[:8]}" in text
    assert sha[:9] not in text                                    # the first 8 hex digits only
    assert "Android asks once to allow installing from Chrome" in text
    assert text.index('id="open"') < text.index('id="app"') < text.index('id="ca"')
    srv = _portal(tmp_path / "none", apk=False)
    st, _, body = _get(srv, "/")
    text = body.decode()
    assert st == 200 and "{{" not in text and "Open K1000" in text and "k1000-ca.crt" in text
    for gone in ("k1000.apk", "Get the K1000 app", 'id="app"', "Android asks", "APK"):
        assert gone not in text, gone
    # no portal.html at all (a broken install): the minimal page still links the app
    srv = _portal(tmp_path / "bare")
    srv._static = {}
    assert b'href="/k1000.apk"' in _get(srv, "/")[2]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run portal.js")
@pytest.mark.parametrize("case", ["chrome", "webview", "no-tls"])
def test_portal_js_hides_the_app_where_it_cannot_work(case: str) -> None:
    """The card is shown in Chrome; hidden in Android's sign-in WebView (Chrome installs the APK)
    and on a box without HTTPS (the app only talks HTTPS)."""
    ua = ("Mozilla/5.0 (Linux; Android 13; Pixel 4a; wv) AppleWebKit/537.36" if case == "webview"
          else "Mozilla/5.0 (Linux; Android 13; Pixel 4a) AppleWebKit/537.36 Chrome/130")
    data = {"app": "https://10.42.0.1/", "ping": "" if case == "no-tls" else "https://10.42.0.1/ping",
            "ca": "1", "tls": "" if case == "no-tls" else "1", "on": "1"}
    harness = r"""
const els = {};
const el = (id) => els[id] || (els[id] = { id, hidden: false, className: "", textContent: "",
  classList: { remove() {} }, replaceWith() {} });
globalThis.document = { body: { dataset: %(data)s }, getElementById: el };
Object.defineProperty(globalThis, "navigator", { value: { userAgent: %(ua)s }, configurable: true });
globalThis.location = { origin: "http://10.42.0.1" };
globalThis.fetch = () => new Promise(() => {});
globalThis.AbortController = class { abort() {} get signal() { return null; } };
globalThis.setTimeout = () => 0; globalThis.clearTimeout = () => {};
el("app");
%(src)s
console.log(JSON.stringify({ app: els.app.hidden, open: els.open.hidden }));
""" % {"data": json.dumps(data), "ua": json.dumps(ua),
       "src": (web.STATIC_DIR / "portal.js").read_text()}
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["app"] is (case != "chrome")


def test_portal_sends_a_large_apk_over_tcp(tmp_path: Path) -> None:
    """Through the real portal socket: the whole APK (bigger than one drain's 5 s budget needs)."""
    async def go() -> None:
        port = free_port()
        folder = tmp_path / "apk"
        body = _put_apk(folder, "0.2.0", size=600 * 1024)
        srv = PhoneServer(object(), settings(tmp_path, http_port=port, plain_app="off", apk_dir=folder),
                          presence_s=3600)
        await srv.start()
        assert srv._apk is not None and srv._apk.body == body
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET /k1000.apk HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 10)
        writer.close()
        st, h, got = _parse(raw)
        assert st == 200 and got == body and int(h["content-length"]) == len(body)
        await srv.stop()
    run(go())


# ======================================================================= HTTPS: /api/wifi and the APK
def _https_get(port: int, path: str, cafile: Path, *, src: str = "127.0.0.2", host: str = "127.0.0.1",
               rcvbuf: int = 0, pause: float = 0.0) -> tuple[int, dict[str, str], bytes]:
    """A blocking HTTPS GET from source address `src` (rcvbuf/pause: a slow phone)."""
    ctx = ssl.create_default_context(cafile=str(cafile))
    raw = socket.socket()
    raw.settimeout(20)
    if rcvbuf:
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    raw.bind((src, 0))
    raw.connect(("127.0.0.1", port))
    data = b""
    with ctx.wrap_socket(raw, server_hostname="localhost") as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
        while True:
            try:
                chunk = s.recv(4096)
            except (ssl.SSLError, ConnectionError, OSError):
                break
            if not chunk:
                break
            data += chunk
            if pause:
                time.sleep(pause)
    return _parse(data)


class _Box:
    """A PhoneServer with HTTPS, the plain page and the portal, on a thread's own event loop."""

    def __init__(self, tmp: Path, tls: Path, **kw: Any) -> None:
        self.tmp, self.tls = tmp, tls
        opts: dict[str, Any] = dict(https_port=free_port(), http_port=free_port(), plain_app="always",
                                    tls_dir=tls, iface="wlan0", apk_dir=tmp / "apk",
                                    on_air_file=tmp / "on-air")
        opts.update(kw)
        self.cfg = settings(tmp, **opts)
        self.loop = asyncio.new_event_loop()
        self.t = [1000.0]
        self.srv = PhoneServer(object(), self.cfg, presence_s=3600, clock=lambda: self.t[0])
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.call(self.srv.start())

    def call(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(30)

    def get(self, path: str, **kw: Any) -> tuple[int, dict[str, str], bytes]:
        return _https_get(self.srv.ports["https"], path, self.tls / "k1000.crt", **kw)

    def close(self) -> None:
        self.call(self.srv.stop())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


@pytest.fixture
def box(tmp_path: Path):
    tls = _self_signed(tmp_path / "tls")
    if tls is None:
        pytest.skip("openssl CLI not available to make a throwaway certificate")
    (tmp_path / "ap.psk").write_text(PSK + "\n")
    _on_air(tmp_path)                                             # the AP runs that password
    (tmp_path / "ap.info").write_text("ssid=K1000-T\nband=a\nchannel=36\nlabel=5 GHz · ch 36 · hostapd\n"
                                      "iface=wlan0\nstate=up\n")
    _arp(tmp_path / "arp", ("127.0.0.2", "wlan0"), ("127.0.0.4", "eth0"), ("127.0.1.5", "wlan0"))
    _put_apk(tmp_path / "apk", "0.2.0", size=96 * 1024)
    b = _Box(tmp_path, tls)
    try:
        yield b
    finally:
        b.close()


def test_api_wifi_gives_the_app_the_wifi_it_is_on(box: _Box) -> None:
    st, h, body = box.get("/api/wifi")
    assert st == 200 and h["content-type"] == "application/json" and h["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in h                 # a web page cannot read it
    got = json.loads(body)
    assert got == {"v": 1, "ssid": "K1000-T", "psk": PSK, "band": "5 GHz · ch 36 · hostapd",
                   "box": box.srv.build}
    assert list(got) == ["v", "ssid", "psk", "band", "box"] and len(box.srv.build) == 12
    assert box.get("/api/wifi", host="localhost")[0] == 200       # the box's other name
    assert box.get("/api/wifi?x=1", host="127.0.0.1:443")[0] == 200
    assert box.srv.stats["wifi_sent"] == 3


def test_api_wifi_only_to_a_device_on_the_ap(box: _Box) -> None:
    # 127.0.1.5: in the neighbour table on the AP interface, but outside the AP's subnet
    st, h, body = box.get("/api/wifi", src="127.0.1.5")
    assert st == 403 and PSK.encode() not in body and h["cache-control"] == "no-store"
    # 127.0.0.4: in the subnet, but its neighbour entry is on eth0 (never the AP)
    assert box.get("/api/wifi", src="127.0.0.4")[0] == 403
    # 127.0.0.3: in the subnet, not in the neighbour table at all
    assert box.get("/api/wifi", src="127.0.0.3")[0] == 403
    # the box's own address (a process on the box)
    assert box.get("/api/wifi", src="127.0.0.1")[0] == 403
    assert box.srv.stats["wifi_refused"] == 4 and box.srv.stats["wifi_sent"] == 0
    # ap.info names the AP interface: the same device, on a dongle that is not wlan0
    (box.tmp / "ap.info").write_text("ssid=K1000-T\niface=wlan1\n")
    assert box.get("/api/wifi")[0] == 403
    _arp(box.tmp / "arp", ("127.0.0.2", "wlan1"))
    assert box.get("/api/wifi")[0] == 200
    assert not box.srv.on_ap("not-an-ip") and not box.srv.on_ap("") and not box.srv.on_ap("::1")
    assert box.srv.on_ap("::ffff:127.0.0.2")                      # a dual-stack socket's form


def test_api_wifi_is_https_only_and_for_the_box_names(box: _Box) -> None:
    import urllib.request
    for port in (box.srv.ports["plain"], box.srv.ports["http"]):   # the plain page, the portal
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/wifi")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 404 and PSK.encode() not in e.value.read()
    assert box.get("/api/wifi", host="attacker.example")[0] == 404
    assert box.srv.stats["wifi_sent"] == 0


def test_api_wifi_404_without_a_password_and_fresh_once_a_new_one_is_on_the_air(box: _Box) -> None:
    (box.tmp / "ap.psk").unlink()
    st, h, body = box.get("/api/wifi")
    assert st == 404 and b"no Wi-Fi password" in body and h["cache-control"] == "no-store"
    (box.tmp / "ap.psk").write_text("short\n")                    # not a WPA2 password
    assert box.get("/api/wifi")[0] == 404
    _new_psk(box.tmp, "a-brand-new-password")                     # setup-ap.sh --new-password
    _on_air(box.tmp)                                              # ... and the AP restarted with it
    assert json.loads(box.get("/api/wifi")[2])["psk"] == "a-brand-new-password"
    (box.tmp / "ap.info").unlink()                                # no ap.info: k1000.yaml's names
    got = json.loads(box.get("/api/wifi")[2])
    assert got["ssid"] == box.cfg.ssid and got["band"] == box.cfg.band


def test_api_wifi_never_hands_out_a_password_the_ap_is_not_running(box: _Box) -> None:
    """Review (APK + /api/wifi) #1: `setup-ap.sh --new-password` writes ap.psk before the AP
    restarts with it (seconds; until the next boot with --no-up). A device still on the AP with
    the old password must not learn the new one meanwhile: 503 until the AP's start records it."""
    assert json.loads(box.get("/api/wifi")[2])["psk"] == PSK
    _new_psk(box.tmp, "rotated-k1000-pass")                       # the same second, a new file
    st, h, body = box.get("/api/wifi")
    assert st == 503 and h["retry-after"] == "5" and h["cache-control"] == "no-store"
    assert b"rotated" not in body and PSK.encode() not in body
    assert box.srv.stats["wifi_held"] == 1 and box.srv.stats["wifi_sent"] == 1
    _on_air(box.tmp)                                              # the AP restarted with it
    assert json.loads(box.get("/api/wifi")[2])["psk"] == "rotated-k1000-pass"
    (box.tmp / "on-air").unlink()                                 # an AP started by older units
    assert box.get("/api/wifi")[0] == 503
    (box.tmp / "on-air").write_text("garbage\n")
    assert box.get("/api/wifi")[0] == 503
    assert box.srv.stats["wifi_sent"] == 2 and box.srv.stats["wifi_held"] == 3
    box.t[0] += 10.0                                              # the per-device bucket refills
    # the order of the refusals: not on the AP (403) and no password (404) come first
    assert box.get("/api/wifi", src="127.0.0.3")[0] == 403
    (box.tmp / "ap.psk").unlink()
    assert box.get("/api/wifi")[0] == 404


def test_psk_on_air_matches_what_the_ap_side_records(tmp_path: Path) -> None:
    """The hostapd unit's ExecStartPost (run as systemd would: %% and $$ unescaped, the real
    stat), the NetworkManager guard and setup-ap.sh record ap.psk in exactly psk_id's form."""
    unit = (NET / "k1000-ap-hostapd.service").read_text()
    m = re.search(r"^ExecStartPost=-/bin/sh -c '(.*)'$", unit, re.M)
    assert m
    psk, on_air = tmp_path / "ap.psk", tmp_path / "on-air"
    script = m.group(1).replace("%%", "%").replace("$$", "$").replace("/etc/k1000/ap.psk", str(psk)) \
        .replace("/run/k1000-ap.on-air", str(on_air))
    psk.write_text(PSK + "\n")
    assert not psk_on_air(psk_id(psk.stat()), on_air)            # nothing recorded yet
    subprocess.run(["/bin/sh", "-c", script], check=True)
    assert psk_on_air(psk_id(psk.stat()), on_air)
    _new_psk(tmp_path, "another-password")
    assert not psk_on_air(psk_id(psk.stat()), on_air)
    assert not psk_on_air("", on_air)
    for f in (NET / "k1000-ap-guard.sh", NET / "setup-ap.sh"):
        assert "stat -c '%i %Y' \"$PSK_FILE\"" in f.read_text(), f.name


def test_api_wifi_is_rate_limited_per_device(box: _Box) -> None:
    rate, burst = web.HTTP_LIMITS["wifi"]
    for _ in range(int(burst)):
        assert box.get("/api/wifi")[0] == 200
    st, h, body = box.get("/api/wifi")
    assert st == 429 and h["retry-after"] == "1" and PSK.encode() not in body
    assert box.get("/api/wifi", src="127.0.0.3")[0] == 403         # another device: its own bucket
    box.t[0] += 1.0 / rate
    assert box.get("/api/wifi")[0] == 200


def test_https_apk(box: _Box) -> None:
    body = (box.tmp / "apk/K1000-0.2.0.apk").read_bytes()
    st, h, got = box.get("/k1000.apk", src="127.0.1.5")           # the APK is for anyone who reaches it
    assert st == 200 and got == body
    assert h["content-type"] == "application/vnd.android.package-archive"
    assert h["content-disposition"] == 'attachment; filename="K1000-0.2.0.apk"'
    assert int(h["content-length"]) == len(body) and h["etag"] == f'"{hashlib.sha256(body).hexdigest()}"'
    st, h, got = _https_get(box.srv.ports["https"], "/", box.tls / "k1000.crt")
    assert st == 200 and b"/app.js" in got                         # the page itself is unchanged
    box.srv._apk = None
    assert box.get("/k1000.apk")[0] == 404


def test_https_apk_reaches_a_slow_phone_whole(box: _Box) -> None:
    """websockets closes a refused handshake 2 s (close_timeout) after its response: an APK to a
    phone at the edge of the Wi-Fi's range takes longer. The APK's connection gets APK_SEND_S
    (within websockets' 10 s open_timeout); /ws keeps its own."""
    body = _put_apk(box.tmp / "apk2", "0.3.0", size=400 * 1024)
    box.srv._apk = load_apk(box.tmp / "apk2")
    for s in box.srv._ws_servers[0].sockets:                      # the HTTPS server: small buffers,
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)   # so the phone's pace sets the time
    t0 = time.monotonic()
    st, h, got = box.get("/k1000.apk", rcvbuf=4096, pause=0.03)
    took = time.monotonic() - t0
    assert st == 200 and len(got) == len(body) and got == body
    assert took > 2.2, f"not slow enough to test anything ({took:.1f} s)"


def test_portal_aborts_an_apk_download_that_runs_out_of_time(tmp_path: Path, monkeypatch: Any) -> None:
    """Review (APK + /api/wifi) #3: a phone that stops reading (out of range) must not keep the
    APK's rest buffered once its time is up: close() alone would keep sending it, with no
    deadline, past the handler and its semaphore slot. The rest is dropped, the socket closed."""
    monkeypatch.setattr(web, "APK_SEND_S", 0.5)

    async def go() -> None:
        port = free_port()
        folder = tmp_path / "apk"
        body = _put_apk(folder, "0.2.0", size=2 * 1024 * 1024)
        srv = PhoneServer(object(), settings(tmp_path, http_port=port, plain_app="off", apk_dir=folder),
                          presence_s=3600)
        await srv.start()
        for s in srv._portal_servers[0].sockets:                   # small buffers: the kernel holds
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)  # little, k1000 the rest
        raw = socket.socket()
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        raw.setblocking(False)
        await asyncio.get_running_loop().sock_connect(raw, ("127.0.0.1", port))
        reader, writer = await asyncio.open_connection(sock=raw, limit=1 << 16)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        writer.write(f"GET /k1000.apk HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        reader._transport.pause_reading()                          # the phone stops reading
        await asyncio.sleep(2.0)                                   # its 0.5 s, and the handler gone
        assert not srv._portal_sem.locked() and srv._portal_sem._value == 32
        reader._transport.resume_reading()
        got = await asyncio.wait_for(reader.read(), 10)            # what was in flight, then EOF
        writer.close()
        assert len(got) < len(body) / 2, len(got)                  # the rest was never sent
        await srv.stop()
    run(go())


def test_portal_sends_a_slow_phone_the_whole_apk_within_its_time(tmp_path: Path, monkeypatch: Any) -> None:
    """The fix for #3 aborts only past the deadline, which covers the tail too: what is still
    buffered when drain() returns goes out within APK_SEND_S, however long it takes (here more
    than the 1 s the handler used to wait for the close)."""
    closes: list[float] = []
    real_close = asyncio.StreamWriter.close

    def close(self: asyncio.StreamWriter) -> None:
        closes.append(time.monotonic())
        real_close(self)
    monkeypatch.setattr(asyncio.StreamWriter, "close", close)

    async def go() -> None:
        port = free_port()
        folder = tmp_path / "apk"
        body = _put_apk(folder, "0.2.0", size=66 * 1024)         # over 64 KiB: the APK's budget
        srv = PhoneServer(object(), settings(tmp_path, http_port=port, plain_app="off", apk_dir=folder),
                          presence_s=3600)
        await srv.start()
        for s in srv._portal_servers[0].sockets:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        raw = socket.socket()
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        raw.setblocking(False)
        await asyncio.get_running_loop().sock_connect(raw, ("127.0.0.1", port))
        reader, writer = await asyncio.open_connection(sock=raw, limit=1024)
        writer.write(f"GET /k1000.apk HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        await writer.drain()
        data = b""
        while chunk := await asyncio.wait_for(reader.read(1024), 10):   # ~20 KB/s
            data += chunk
            await asyncio.sleep(0.05)
        eof = time.monotonic()
        writer.close()
        st, h, got = _parse(data)
        assert st == 200 and got == body
        assert closes and eof - closes[0] > 1.0, "the tail was not slow enough to test anything"
        await srv.stop()
    run(go())


def test_an_ap_less_box_offers_the_app_but_never_the_wifi(tmp_path: Path) -> None:
    """No neighbour table readable (no AP): the Wi-Fi settings are refused, the APK still served."""
    tls = _self_signed(tmp_path / "tls")
    if tls is None:
        pytest.skip("openssl CLI not available to make a throwaway certificate")
    (tmp_path / "ap.psk").write_text(PSK + "\n")
    _put_apk(tmp_path / "apk", "0.2.0")
    b = _Box(tmp_path, tls)
    try:
        assert b.get("/api/wifi")[0] == 403
        assert b.get("/k1000.apk")[0] == 200
    finally:
        b.close()


# ======================================================================= the v1 service worker
SW_HARNESS = r"""
const listeners = {};
globalThis.self = { addEventListener: (t, f) => { listeners[t] = f; },
                    location: { origin: "https://10.42.0.1" }, skipWaiting() {}, clients: { claim() {} } };
const store = new Map();                              // the worker's cache: key -> response
const cache = { match: async (k) => store.get(k), put: async (k, r) => { store.set(k, r); },
                addAll: async () => {} };
globalThis.caches = { open: async () => cache, keys: async () => [], delete: async () => true,
                      match: async (req) => store.get(new URL(req.url).pathname) };
const res = (ctype, body) => ({ ok: true, status: 200, type: "basic", body,
  headers: new Headers({ "Content-Type": ctype }), clone() { return this; } });
const net = {
  "/": res("text/html; charset=utf-8", "fresh page"),
  "/app.js": res("text/javascript; charset=utf-8", "app"),
  "/api/wifi": res("application/json", '{"psk":"secret"}'),
  "/k1000.apk": res("application/vnd.android.package-archive", "PK..."),
  "/tune": res("text/html; charset=utf-8", "tune page"),
};
let fetched = [];
globalThis.fetch = async (req) => { const p = new URL(req.url).pathname; fetched.push(p);
  return net[p] || { ok: false, status: 404, type: "basic", headers: new Headers(), clone() { return this; } }; };
%(src)s
async function go(path, mode) {
  fetched = [];
  const ev = { request: { method: "GET", url: "https://10.42.0.1" + path, mode },
               responded: null, waited: [], respondWith(p) { this.responded = p; },
               waitUntil(p) { this.waited.push(p); } };
  listeners.fetch(ev);
  const r = ev.responded ? await ev.responded : null;
  await Promise.all(ev.waited);
  await new Promise((ok) => setTimeout(ok, 0));
  return { answered: ev.responded !== null, body: r ? r.body : null, fetched };
}
(async () => {
  store.set("/", res("text/html; charset=utf-8", "cached page"));
  const out = {};
  for (const [name, path, mode] of [
      ["wifi", "/api/wifi", "navigate"], ["apk", "/k1000.apk", "navigate"], ["tune", "/tune", "navigate"],
      ["root", "/", "navigate"], ["query", "/?from=app", "navigate"], ["index", "/index.html", "navigate"],
      ["v2", "/v2/", "navigate"], ["js", "/app.js", "no-cors"]]) {
    out[name] = await go(path, mode);
    out[name].cachedRoot = store.get("/").body;
  }
  net["/"] = res("application/json", '{"psk":"secret"}');   // whatever "/" ever answers, not HTML
  out.notHtml = await go("/", "navigate");
  out.notHtml.cachedRoot = store.get("/").body;
  out.keys = [...store.keys()].sort();
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run sw.js")
def test_v1_worker_leaves_other_addresses_alone_and_caches_only_the_page() -> None:
    """Review (APK + /api/wifi) #2: v1's worker answered EVERY navigation with its cached "/" and
    stored what the address returned as "/" (Cache.put ignores no-store): https://10.42.0.1/
    api/wifi opened in a browser with the web app installed put the password where the next
    launch shows it, and /k1000.apk never downloaded. Only "/" (and /index.html) is the page now,
    and only HTML is ever stored under "/"."""
    harness = SW_HARNESS % {"src": (web.STATIC_DIR / "sw.js").read_text()}
    r = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    for name in ("wifi", "apk", "tune", "v2"):                    # straight to the network
        assert not out[name]["answered"] and out[name]["fetched"] == [], (name, out[name])
    for name in ("wifi", "apk", "tune"):                          # "/" untouched by them
        assert out[name]["cachedRoot"] == "cached page", name
    assert out["root"]["answered"] and out["root"]["body"] == "cached page"   # cache first,
    assert out["root"]["cachedRoot"] == "fresh page"                         # then refreshed
    for name in ("query", "index"):                               # the same page
        assert out[name]["answered"] and out[name]["body"] == "fresh page", (name, out[name])
    assert out["query"]["fetched"] == ["/"] and out["index"]["fetched"] == ["/index.html"]
    assert out["js"]["answered"] and out["js"]["fetched"] == ["/app.js"]
    assert out["notHtml"]["cachedRoot"] == "fresh page"           # never stored under "/"
    assert out["keys"] == ["/", "/app.js"]                         # nothing else kept


# ======================================================================= install.sh step 5b
FAKE_INSTALL = r'''
import os, shutil, sys
a = sys.argv[1:]; mode = None; owner = group = None; d = False; files = []
while a:
    x = a.pop(0)
    if x == "-m": mode = int(a.pop(0), 8)
    elif x == "-o": owner = a.pop(0)
    elif x == "-g": group = a.pop(0)
    elif x == "-d": d = True
    else: files.append(x)
def own(p):
    q = os.path.join(os.environ["OWNERS"], os.path.abspath(p).replace("/", "%"))
    open(q, "w").write(f"{owner or 'root'}:{group or 'root'}\n")
if d:
    for p in files:
        os.makedirs(p, exist_ok=True)
        if mode is not None: os.chmod(p, mode)
        own(p)
else:
    src, dst = files
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o755 if mode is None else mode)
    own(dst)
'''

# stat -c FORMAT FILE with %a, %U and %G (owner as the fake install recorded it)
FAKE_STAT = r'''
import os, sys
fmt, f = sys.argv[2], sys.argv[3]
if not os.path.exists(f): sys.exit(1)
q = os.path.join(os.environ["OWNERS"], os.path.abspath(f).replace("/", "%"))
u, g = (open(q).read().strip() if os.path.exists(q) else "user:user").split(":")
print(fmt.replace("%a", format(os.stat(f).st_mode & 0o777, "o")).replace("%U", u).replace("%G", g))
'''


def _bin(d: Path, name: str, body: str, shebang: str = "#!/bin/sh") -> None:
    p = d / name
    p.write_text(shebang + "\n" + body)
    p.chmod(0o755)


class _Install:
    """install.sh's put/own_dir helpers + step 5b, with APP and SRC in tmp and fakes on PATH."""

    def __init__(self, tmp: Path, k1000_active: bool = False) -> None:
        text = INSTALL.read_text()
        helpers = text[text.index("# put SRC DST MODE"):text.index("# revision_of SRC")]
        step = text[text.index("# --- 5b. the Android app"):text.index("# --- 6. systemd")]
        self.src, self.app = tmp / "src", tmp / "opt-k1000"
        self.src.mkdir()
        self.app.mkdir()
        fb, owners = tmp / "bin", tmp / "owners"
        fb.mkdir()
        owners.mkdir()
        _bin(fb, "install", FAKE_INSTALL, "#!" + sys.executable)
        _bin(fb, "stat", FAKE_STAT, "#!" + sys.executable)
        _bin(fb, "chown", "exit 0\n")
        _bin(fb, "systemctl", "exit 0\n" if k1000_active else "exit 3\n")
        self.prog = ('set -euo pipefail\nCHANGES=0\nDO_START=0\n'
                     f'SRC="{self.src}"\nAPP="{self.app}"\n'
                     'note() { printf "%s\\n" "$*"; }\n'
                     'changed() { CHANGES=$((CHANGES + 1)); note "$*"; }\n'
                     'die() { printf "ERROR: %s\\n" "$*" >&2; exit 1; }\n'
                     + helpers + step + 'echo "CHANGES=$CHANGES"\n')
        self.env = dict(os.environ, PATH=f"{fb}:/usr/bin:/bin", OWNERS=str(owners))
        self.owners = owners

    def run(self) -> tuple[str, int]:
        r = subprocess.run(["bash", "-c", self.prog], capture_output=True, text=True, env=self.env)
        assert r.returncode == 0, r.stdout + r.stderr
        m = re.search(r"^CHANGES=(\d+)$", r.stdout, re.M)
        assert m, r.stdout
        return r.stdout, int(m.group(1))

    def owner(self, p: Path) -> str:
        return (self.owners / str(p.resolve()).replace("/", "%")).read_text().strip()

    def installed(self) -> list[str]:
        d = self.app / "apk"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_install_puts_the_newest_apk_only_root_owned_0644(tmp_path: Path) -> None:
    ins = _Install(tmp_path)
    _put_apk(ins.src / "apk", "0.2.0")
    newest = _put_apk(ins.src / "apk", "0.10.0")
    _put_apk(ins.src / "apk", "0.9.1")
    (ins.src / "apk/K1000-0.10.0.apk.sha256").write_text("x\n")
    (ins.src / "apk/app-bench.apk").write_bytes(_apk_bytes(1024))
    (ins.src / "apk/K1000-latest.apk").write_bytes(_apk_bytes(1024))
    out, n = ins.run()
    assert ins.installed() == ["K1000-0.10.0.apk"], out
    f = ins.app / "apk/K1000-0.10.0.apk"
    assert f.read_bytes() == newest and oct(f.stat().st_mode & 0o777) == "0o644"
    assert ins.owner(f) == "root:root" and ins.owner(ins.app / "apk") == "root:root"
    assert oct((ins.app / "apk").stat().st_mode & 0o777) == "0o755"
    assert "app-bench.apk ignored" in out and "K1000-latest.apk ignored" in out
    assert n == 2                                                  # the folder + the APK
    out, n = ins.run()
    assert n == 0, out                                             # idempotent


def test_install_removes_stale_apks_and_follows_a_rollback(tmp_path: Path) -> None:
    ins = _Install(tmp_path, k1000_active=True)
    _put_apk(ins.src / "apk", "0.10.0")
    ins.run()
    (ins.app / "apk/K1000-0.1.0.apk").write_bytes(_apk_bytes(1024, "old"))   # by hand, or older
    (ins.app / "apk/notes.txt").write_text("x\n")
    out, n = ins.run()
    assert ins.installed() == ["K1000-0.10.0.apk"] and n == 2, out
    # a rollback copy carries an older APK: the box offers what it was deployed with
    shutil.rmtree(ins.src / "apk")
    older = _put_apk(ins.src / "apk", "0.2.0")
    out, n = ins.run()
    assert ins.installed() == ["K1000-0.2.0.apk"] and (ins.app / "apk/K1000-0.2.0.apk").read_bytes() == older
    assert n == 2 and "once k1000 has started again" in out       # k1000 loads it at its start


def test_install_without_an_apk_keeps_the_boxs(tmp_path: Path) -> None:
    ins = _Install(tmp_path)
    out, n = ins.run()                                             # no apk/ at all, nothing on the box
    assert n == 0 and ins.installed() == [] and "offers none" in out
    _put_apk(ins.src / "apk", "0.2.0")
    ins.run()
    shutil.rmtree(ins.src / "apk")                                 # a deploy without the APK
    out, n = ins.run()
    assert n == 0 and ins.installed() == ["K1000-0.2.0.apk"]
    assert "keeps offering K1000-0.2.0.apk" in out
    (ins.src / "apk").mkdir()
    (ins.src / "apk/K1000-0.3.0.apk").write_text("<html>a failed download</html>\n")
    out, n = ins.run()                                             # not a zip: not installed
    assert n == 0 and ins.installed() == ["K1000-0.2.0.apk"] and "is not an APK" in out


def test_install_mirror_leaves_apk_to_step_5b(tmp_path: Path) -> None:
    """Step 3's rsync excludes /apk: it never copies several APKs, and its --delete never removes
    the box's APK when the source has none (run with the script's own filter rules)."""
    text = INSTALL.read_text()
    m = re.search(r"RSYNC=\(rsync (.*?)\)\n", text, re.S)
    assert m and "--exclude=/apk" in m.group(1)
    assert text.index("# --- 3. the application") < text.index("# --- 5b. the Android app") \
        < text.index("# --- 6. systemd")
    if shutil.which("rsync") is None:
        pytest.skip("no rsync")
    excludes = [a.replace("'", "") for a in re.findall(r"--exclude=\S+", m.group(1))]
    src, app = tmp_path / "src", tmp_path / "app"
    (src / "k1000").mkdir(parents=True)
    (src / "k1000/x.py").write_text("x\n")
    _put_apk(src / "apk", "0.2.0")
    _put_apk(src / "apk", "0.3.0")
    _put_apk(app / "apk", "0.1.0")
    r = subprocess.run(["rsync", "-rlptO", "--delete", *excludes, f"{src}/", f"{app}/"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (app / "k1000/x.py").exists()
    assert sorted(p.name for p in (app / "apk").iterdir()) == ["K1000-0.1.0.apk"]


def test_apk_names_agree_between_install_and_k1000() -> None:
    m = re.search(r"^APK_RE='\^(.*)\$'$", INSTALL.read_text(), re.M)
    assert m
    for name in ("K1000-0.2.0.apk", "K1000-1.0.0-rc1.apk", "K1000-0.2.apk", "K1000-x.apk",
                 "K1000-.apk", "app-bench.apk", "K1000-0.2.0.apk.sha256", "K1000-0 2.apk",
                 "K1000-" + "1" * 33 + ".apk"):
        assert bool(re.fullmatch(m.group(1), name)) == bool(web.APK_NAME_RE.fullmatch(name)), name


def test_box_apk_is_gitignored() -> None:
    if shutil.which("git") is None or not (BOX.parent / ".git").exists():
        pytest.skip("not a git checkout")
    r = subprocess.run(["git", "-C", str(BOX.parent), "check-ignore", "-q", "box/apk/K1000-0.2.0.apk"])
    assert r.returncode == 0
