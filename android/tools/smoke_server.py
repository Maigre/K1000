#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stand-in for the box in the emulator smoke test: serves the K1000 PWA's static files over
HTTPS with a leaf from a throwaway test CA, and logs every request (one JSON line each), so the
test can tell what the app's WebView really fetched and over which TLS certificate.

    smoke_server.py --cert leaf.pem --key leaf.key --static <box/k1000/web/static> --log req.jsonl

Paths: "/" = index.html, "/sw.js" with its build stamp filled in, "/probe" = a tiny no-store
answer for network checks; "/ws" answers 404 (the page then shows its own "not connected").
"/v2/" (app 0.2's default page): --v2 test (default) = tools/bridge_test.html, the page side of
android/BRIDGE.md; --v2 none = 404, as a box without the v2 page; --v2 <dir> = that directory.
"/api/wifi" (app 0.2.1 takes the K1000 Wi-Fi from the box): --wifi none (default) = 404, as a box
older than that; --wifi <ssid>:<password> = the box's answer, {"v":1,"ssid":…,"psk":…,"band":…,
"box":…} with Cache-Control: no-store (test values only, never a real password). Faults (review
2026-10-13): --tls-cut N = the first N connections are closed in the middle of the TLS handshake,
once the whole ClientHello is in (a FIN, no ServerHello: Android's TLS then fails with "connection
closed", an SSLException that is no certificate failure; on the Connect screen the app's /api/wifi
request is the only HTTPS client); --wifi-reset N = the first N /api/wifi requests get a TCP reset once the request
is read, inside TLS, no answer (the edge of Wi-Fi range, the box's service restarting); --wifi-slow S
= the answer's body trickles in over about S seconds, every gap under the app's 4 s read timeout
(a slow box: the request stays running that long).
"""
import argparse
import json
import mimetypes
import socket
import ssl
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--static", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--build", default="smoke-1")
    ap.add_argument("--v2", default="test", help="test | none | <directory of a v2 page>")
    ap.add_argument("--wifi", default="none", help="none | <ssid>:<password> (GET /api/wifi)")
    ap.add_argument("--tls-cut", type=int, default=0, help="the first N connections: closed mid-handshake")
    ap.add_argument("--wifi-reset", type=int, default=0, help="the first N /api/wifi: TCP reset, no answer")
    ap.add_argument("--wifi-slow", type=float, default=0, help="the /api/wifi body over about S seconds")
    a = ap.parse_args()
    resets = {"left": a.wifi_reset, "cuts": a.tls_cut}
    lock = threading.Lock()
    wifi = None
    if a.wifi != "none":
        ssid, _, psk = a.wifi.partition(":")
        wifi = json.dumps({"v": 1, "ssid": ssid, "psk": psk, "band": "bg", "box": a.build}).encode()
    root = Path(a.static).resolve()
    bridge_test = Path(__file__).resolve().parent / "bridge_test.html"
    v2_root = None if a.v2 in ("test", "none") else Path(a.v2).resolve()
    log = open(a.log, "a", buffering=1)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # One request per connection: a network-binding check must not ride on a kept-alive socket.

        def log_message(self, fmt, *args):  # quiet stderr; the JSON log is the record
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _reset(self):
            # SO_LINGER 0: the socket's real close (when the handler's files close) sends a RST,
            # with no TLS close_notify and no FIN first: the client's read fails inside TLS.
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.close_connection = True
            self.connection.close()

        def _trickle(self, body, seconds):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.flush()
            pieces = max(2, int(seconds / 2) + 1)   # gaps of at most 2 s
            step = -(-len(body) // pieces)
            for i in range(0, len(body), step):
                time.sleep(seconds / pieces)
                self.wfile.write(body[i:i + step])
                self.wfile.flush()

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            log.write(json.dumps({"t": time.time(), "path": self.path, "client": self.client_address[0],
                                  "ua": self.headers.get("User-Agent", ""),
                                  "sw": self.headers.get("Service-Worker", "")}) + "\n")
            if path == "/probe":
                return self._send(200, b"probe ok", "text/plain", {"Cache-Control": "no-store"})
            if path == "/api/wifi":
                with lock:
                    reset = resets["left"] > 0
                    resets["left"] -= reset
                if reset:
                    return self._reset()
                if wifi is None:
                    return self._send(404, b"not found\n", "text/plain; charset=utf-8")
                if a.wifi_slow > 0:
                    return self._trickle(wifi, a.wifi_slow)
                return self._send(200, wifi, "application/json", {"Cache-Control": "no-store"})
            if path in ("/ws", "/v2/ws"):
                return self._send(404, b"no websocket in the smoke test", "text/plain")
            base = root
            if path == "/v2" or path.startswith("/v2/"):
                if a.v2 == "none":
                    return self._send(404, b"not found\n", "text/plain; charset=utf-8")
                if a.v2 == "test":
                    if path in ("/v2/", "/v2/index.html"):
                        return self._send(200, bridge_test.read_bytes(), "text/html; charset=utf-8",
                                          {"Cache-Control": "no-cache"})
                    return self._send(404, b"not found\n", "text/plain; charset=utf-8")
                base, path = v2_root, path[len("/v2"):] or "/"
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            f = (base / name).resolve()
            if base not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            body = f.read_bytes()
            if name == "sw.js" or name.endswith("/sw.js"):
                body = body.replace(b"__K1000_BUILD__", a.build.encode())
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if name.endswith(".webmanifest"):
                ctype = "application/manifest+json"
            self._send(200, body, ctype, {"Cache-Control": "no-cache"})

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(a.cert, a.key)

    class TLSServer(ThreadingHTTPServer):
        def get_request(self):
            # TLS on each accepted connection (an error here, a refused handshake included, only
            # drops that connection), or, for the first --tls-cut ones, a close mid-handshake.
            raw, addr = self.socket.accept()
            with lock:
                cut = resets["cuts"] > 0
                resets["cuts"] -= cut
            if cut:
                self._cut(raw)
                log.write(json.dumps({"t": time.time(), "path": "(tls cut)", "client": addr[0]}) + "\n")
                raise OSError("TLS handshake cut on purpose")
            return ctx.wrap_socket(raw, server_side=True), addr

        @staticmethod
        def _cut(raw):
            # Read the whole ClientHello record (unread data would turn the close into a RST), then
            # a FIN instead of the ServerHello.
            raw.settimeout(3)
            try:
                head = b""
                while len(head) < 5:
                    d = raw.recv(5 - len(head))
                    if not d:
                        break
                    head += d
                left = int.from_bytes(head[3:5], "big") if len(head) == 5 else 0
                while left > 0:
                    d = raw.recv(min(left, 4096))
                    if not d:
                        break
                    left -= len(d)
                raw.shutdown(socket.SHUT_WR)
                while raw.recv(4096):   # until the client closes its side (or the timeout)
                    pass
            except OSError:
                pass
            raw.close()

    srv = TLSServer(("0.0.0.0", a.port), Handler)
    print(f"smoke server on :{a.port} with {a.cert}", file=sys.stderr, flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
