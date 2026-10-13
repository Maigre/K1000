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
"""
import argparse
import json
import mimetypes
import ssl
import sys
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
    a = ap.parse_args()
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

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            log.write(json.dumps({"t": time.time(), "path": self.path, "client": self.client_address[0],
                                  "ua": self.headers.get("User-Agent", ""),
                                  "sw": self.headers.get("Service-Worker", "")}) + "\n")
            if path == "/probe":
                return self._send(200, b"probe ok", "text/plain", {"Cache-Control": "no-store"})
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
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    print(f"smoke server on :{a.port} with {a.cert}", file=sys.stderr, flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
