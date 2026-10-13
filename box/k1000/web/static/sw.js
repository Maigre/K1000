// SPDX-License-Identifier: GPL-3.0-or-later
/* K1000 service worker: the app shell opens instantly from the cache, with or without the box.
   The live data never goes through here (it is a websocket). The server stamps BUILD with a hash
   of every other file, so any change to the page installs a fresh cache. */
"use strict";
const BUILD = "__K1000_BUILD__";
const CACHE = "k1000-" + BUILD;
const SHELL = [
  "/", "/app.js", "/app.css", "/manifest.webmanifest",
  "/icons/icon-192.png", "/icons/icon-512.png", "/icons/maskable-192.png", "/icons/maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("k1000-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Cache first for the shell (instant start, works offline), refreshed in the background when
// the box answers. Anything else: network, then cache.
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // /v2/ (the phone v2 page) has its own worker: never answer it with v1's cached "/".
  if (url.origin !== self.location.origin || url.pathname === "/ws" || url.pathname === "/v2" ||
      url.pathname.startsWith("/v2/")) return;
  const key = req.mode === "navigate" ? "/" : url.pathname;
  if (SHELL.includes(key)) {
    event.respondWith(
      caches.open(CACHE).then((cache) => cache.match(key, { ignoreSearch: true }).then((hit) => {
        const fresh = fetch(req).then((res) => {
          if (res && res.ok && res.type === "basic") cache.put(key, res.clone());
          return res;
        }).catch(() => null);
        if (hit) {
          event.waitUntil(fresh);
          return hit;
        }
        return fresh.then((res) => res || offline());
      })),
    );
    return;
  }
  event.respondWith(fetch(req).catch(() => caches.match(req).then((hit) => hit || offline())));
});

function offline() {
  return new Response(
    "<!doctype html><meta name=viewport content='width=device-width'><body style='background:#000;color:#fff;font:20px system-ui;padding:24px'>" +
    "<b>Not connected to K1000</b><p>Join the K1000 Wi-Fi (scan the QR on the box) and reopen.</p>",
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
}
