// SPDX-License-Identifier: GPL-3.0-or-later
/* The v2 page's service worker, scope /v2/ (registered from /v2/ as "sw.js"; the box serves it
   with Service-Worker-Allowed: /v2/ and stamps BUILD with the v2 build id, so every v2 deploy
   installs a fresh cache). docs/14 §1:
   - the shell is cached at INSTALL (cache.addAll: cors requests, no Vary), because a navigation
     response under /v2/ carries `Vary: *`, which Cache.put refuses. Each request bypasses the
     HTTP cache (cache: "reload"): a revalidated "./" (ETag, 304) would keep the stored
     navigation's `Vary: *`, addAll would reject it and the worker would never install;
   - network first, with a short timeout: on the K1000 Wi-Fi the page is always the box's
     current one (never a mix of an old and a new file); off it, the cached shell opens and says
     NO LINK with the real fallback, instead of the browser's error page;
   - the websocket never goes through here, and nothing outside /v2/ is touched. */
"use strict";
const BUILD = "__K1000_BUILD__";
const CACHE = "k1000v2-" + BUILD;
const SHELL = ["./", "app.css", "js/main.js", "js/util.js", "js/fields.js", "js/store.js", "js/link.js", "js/native.js",
  "js/haptics.js", "js/view.js", "js/hold.js", "js/touch.js", "js/nudge.js", "js/tile.js", "js/stepper.js", "js/bands.js",
  "js/graph.js", "js/rta.js", "js/mute.js", "js/sheets.js", "js/micmath.js", "js/mic.js", "js/measure.js"];
const NET_MS = 2500;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE)
    .then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" }))))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k.startsWith("k1000v2-") && k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

const scope = () => new URL(self.registration.scope);
function cachedFor(req) {
  const url = new URL(req.url);
  const key = req.mode === "navigate" || url.pathname === scope().pathname + "index.html" ? scope().href : req.url;
  return caches.open(CACHE).then((c) => c.match(key, { ignoreSearch: true }));
}
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith(scope().pathname) || url.pathname.endsWith("/ws")) return;
  event.respondWith(new Promise((resolve) => {
    let done = false;
    const fallback = () => cachedFor(req).then((hit) => hit || offline(req)).catch(() => offline(req));
    const timer = setTimeout(() => { if (!done) { done = true; fallback().then(resolve); } }, NET_MS);
    fetch(req).then((res) => {
      if (done) return;
      done = true; clearTimeout(timer);
      if (res && (res.ok || res.status === 304)) resolve(res);
      else fallback().then((hit) => resolve(hit && hit.status !== 503 ? hit : res));
    }).catch(() => {
      if (done) return;
      done = true; clearTimeout(timer);
      fallback().then(resolve);
    });
  }));
});

function offline(req) {
  if (req.mode !== "navigate") return new Response("", { status: 504 });
  return new Response(
    "<!doctype html><meta name=viewport content='width=device-width'><title>K1000 v2</title>" +
    "<body><h1>Not connected to K1000</h1><p>Join the K1000 Wi-Fi and open this page again.</p>",
    { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
}
