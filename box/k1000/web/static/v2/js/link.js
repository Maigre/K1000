// SPDX-License-Identifier: GPL-3.0-or-later
/* The link to the box: one websocket to /v2/ws (docs/14 §2): a ping every second (the box's
   spectrum subscription wants one at least every 2 s; one a second also lets the page see a
   stalled link after store.SLOW_MS and refuse edits, store.canEdit), DEAD after 6.5 s without a
   frame, back-off 0.5 s doubling to 3 s, 3 s after a 4xxx close, reset on `online` and when the
   page becomes visible. A new connection is a new session: nothing sent before a drop is
   replayed, and every reply still awaited is answered "link lost" at once (DESIGN §9.3). `lost`
   is said once per session that had a state, never again at each failed retry.

   The page knows its own build (the box stamps BUILD below into every .js under /v2/) and
   reloads itself once when the box's hello says another, but only when no finger has been down
   for 2 s and no mute or hold is pending (DESIGN §9.11). */

import { K, UI, cleanHello, cleanState, render, linkSlow } from "./store.js";
import { sget, sset } from "./util.js";
import { onReady } from "./native.js";

export const BUILD = "__K1000_BUILD__";
const UNSTAMPED = "__K1000_" + "BUILD__";      // (split: the stamp replaces every whole mark)
export const PROTOCOL = 2;
const PING_MS = 1000, DEAD_MS = 6500, CONNECT_MS = 4000, IDLE_RELOAD_MS = 2000;

let ws = null, open = false, lastRx = 0, retryMs = 500, retryTimer = 0, connectTimer = 0;
let live = false;                   // this connection got a state: its loss is said (once)
// edits the page refuses on a stalled link (send's last line; the gestures say it first)
const EDITS = new Set(["nudge", "band", "grab", "choose", "act", "unlock"]);
let idSeq = 0;
const pending = new Map();          // id -> {cb, timer}
const handlers = {};
export const stats = { connects: 0, sent: 0, t: {}, log: [] };   // log: the last sends [t, f, page ms]

// the last bad link state: shown (alert line, strip) while a new connection has no state yet,
// so NO LINK / OFF do not blink off at each retry
function bad(v) { K.link = v; K.lastBad = v; }
export function on(t, fn) { (handlers[t] = handlers[t] || []).push(fn); }
function emit(t, m) {
  for (const f of handlers[t] || []) {
    try { f(m); } catch (e) { setTimeout(() => { throw e; }, 0); }
  }
}
export const isOpen = () => open && !!ws;
export const lastHeard = () => lastRx;

function wsUrl() { return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/v2/ws"; }

export function connect() {
  clearTimeout(retryTimer); retryTimer = 0;
  if (ws) return;
  let sock;
  try { sock = new WebSocket(wsUrl()); } catch (e) { schedule(); return; }
  ws = sock;
  // (K.link stays what it was while retrying: NO LINK keeps naming the fallback, OFF stays OFF)
  connectTimer = setTimeout(() => { if (ws === sock && !open) { drop("timeout"); schedule(); } }, CONNECT_MS);
  sock.onopen = () => {
    if (ws !== sock) return;
    clearTimeout(connectTimer);
    open = true; lastRx = K.heard = Date.now(); stats.connects++;     // the back-off resets on the hello, not here
    K.link = "ok";
    K.fresh = false;            // the last state is the old session's: no control until a new one
    emit("open");
    render();
  };
  sock.onmessage = (ev) => {
    if (ws !== sock) return;
    const was = linkSlow();
    lastRx = K.heard = Date.now();
    onMessage(ev.data);
    if (was) render();                    // LINK SLOW clears at once
  };
  sock.onclose = (ev) => {
    if (ws !== sock) return;
    const code = ev.code;
    drop("close");
    if (code === 4003) bad("off");
    else if (code === 4002) bad("busy");
    render();
    schedule(code >= 4000 ? 3000 : 0);
  };
  sock.onerror = () => { /* onclose follows */ };
}

// The link is gone: everything that waited for the box is told so, nothing is kept to resend.
function drop(why) {
  clearTimeout(connectTimer);
  const was = live;
  live = false;
  if (ws) {
    ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
    try { ws.close(); } catch (e) { /* already closed */ }
  }
  ws = null; open = false;
  if (K.link === "ok" || K.link === "connecting") bad(K.lastBad && K.lastBad !== "nolink" && !K.fresh ? K.lastBad : "nolink");
  const waiting = Array.from(pending.entries());
  pending.clear();
  for (const [, p] of waiting) {
    clearTimeout(p.timer);
    try { p.cb(false, { code: "LINK", why: "LINK LOST" }); } catch (e) { setTimeout(() => { throw e; }, 0); }
  }
  if (was) emit("lost", why);
  render();
}

function schedule(min = 0) {
  if (retryTimer) return;
  const ms = Math.max(min, retryMs);
  retryMs = Math.min(retryMs * 2, 3000);
  retryTimer = setTimeout(() => { retryTimer = 0; connect(); }, ms);
}
function reconnectNow() { retryMs = 500; clearTimeout(retryTimer); retryTimer = 0; connect(); }

setInterval(() => {
  if (!open) return;
  if (Date.now() - lastRx > DEAD_MS) { drop("dead"); schedule(); return; }
  send({ t: "ping", ts: Date.now() });
}, PING_MS);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    if (open && Date.now() - lastRx > DEAD_MS) drop("dead");
    if (!ws) reconnectNow();
  }
  emit("visibility", !document.hidden);
});
window.addEventListener("online", () => { if (!ws) reconnectNow(); });

// Fire and forget. False when there is no link (the caller says so; nothing is queued).
export function send(obj) {
  if (!open || !ws) return false;
  if (EDITS.has(obj.t) && linkSlow()) return false;   // never into a stalled link (the caller said it)
  try { ws.send(JSON.stringify(obj)); } catch (e) { return false; }
  stats.sent++; stats.t[obj.t] = (stats.t[obj.t] || 0) + 1;
  if (obj.t !== "ping") {                     // [type, what (a nudge's path, a band's s), page ms]
    stats.log.push([obj.t, obj.t === "nudge" || obj.t === "choose" ? obj.f : obj.s != null ? obj.s : obj.a || "", Math.round(performance.now())]);
    if (stats.log.length > 200) stats.log.shift();
  }
  return true;
}
// With an id: cb(ok, reply) on the box's ack / err, on timeout ({code:"TIMEOUT"}) or on a link
// loss ({code:"LINK"}). Returns the id, or null when nothing was sent.
export function request(obj, cb, timeoutMs = 0) {
  const id = obj.id != null ? obj.id : ++idSeq;
  const msg = Object.assign({}, obj, { id });
  if (pending.has(id)) { const old = pending.get(id); clearTimeout(old.timer); pending.delete(id); }
  if (!send(msg)) return null;
  const p = { cb: cb || (() => {}), timer: 0 };
  if (timeoutMs > 0) {
    p.timer = setTimeout(() => {
      if (pending.get(id) !== p) return;
      pending.delete(id);
      p.cb(false, { code: "TIMEOUT", why: "NO ANSWER" });
    }, timeoutMs);
  }
  pending.set(id, p);
  return id;
}
export const nextId = () => ++idSeq;

function settle(m, ok) {
  const p = pending.get(m.id);
  if (!p) return false;
  pending.delete(m.id);
  clearTimeout(p.timer);
  p.cb(ok, m);
  return true;
}

function onMessage(data) {
  let m;
  try { m = JSON.parse(data); } catch (e) { return; }
  if (!m || typeof m !== "object") return;
  switch (m.t) {
    case "hello":
      m = cleanHello(m);
      K.hello = m;
      retryMs = 500;              // the box really serves this page: a later drop retries fast again
      checkHello(m);
      emit("hello", m);
      break;
    case "state":
      m = cleanState(m);
      K.state = m;
      K.fresh = true;
      live = true;
      K.lastBad = null;
      if (K.link === "connecting") K.link = "ok";
      emit("state", m);
      render();
      break;
    case "m":
      K.meters = m;
      emit("m", m);
      break;
    case "rta":
    case "pgm":                   // RESPONSE's program windows (measure.js), subscribers only
      emit(m.t, m);
      break;
    case "pong":
      if (typeof m.ts === "number") K.rtt = Math.max(0, Date.now() - m.ts);
      break;
    case "ack":
      if (!settle(m, true)) emit("ack", m);
      break;
    case "err":
      if (!settle(m, false)) emit("err", m);
      break;
    case "locked":
    case "unsub":
      emit(m.t, m);
      break;
    case "bye":
      if (m.code === 4003 || m.phone_on === false) bad("off");
      else if (m.code === 4002) bad("busy");
      render();
      break;
    default:
      break;
  }
}

// ------------------------------------------------------------------ box updated / old page
let fingers = 0, lastFinger = 0;
window.addEventListener("pointerdown", () => { fingers++; lastFinger = Date.now(); }, true);
const fingerUp = () => { fingers = Math.max(0, fingers - 1); lastFinger = Date.now(); };
window.addEventListener("pointerup", fingerUp, true);
window.addEventListener("pointercancel", fingerUp, true);
let busy = () => false;
export function setBusyCheck(fn) { busy = fn; }
export const reloadState = { outdated: "", behind: false };

function checkHello(m) {
  if (m.v !== PROTOCOL) {
    if (sget("k1000v2.reload") === "v" + m.v) { bad("old"); reloadState.behind = true; return; }
    bad("old");
    reloadWhenIdle("v" + m.v);
    return;
  }
  if (BUILD === UNSTAMPED || typeof m.build !== "string" || !m.build || m.build === BUILD) {
    reloadState.outdated = ""; reloadState.behind = false;
    return;
  }
  if (sget("k1000v2.reload") === m.build) { reloadState.behind = true; return; }   // reloaded once already
  reloadState.outdated = m.build;
  reloadWhenIdle(m.build);
}
let reloadArmed = false;
async function reloadWhenIdle(tag) {
  if (reloadArmed) return;
  reloadArmed = true;
  render();
  try {
    if ("serviceWorker" in navigator && navigator.serviceWorker.controller) {
      const reg = await navigator.serviceWorker.getRegistration();
      if (reg) await Promise.race([reg.update(), new Promise((res) => setTimeout(res, 3000))]);
    }
  } catch (e) { /* reload anyway: the worker fetches the network first */ }
  const go = () => {
    const idle = fingers === 0 && Date.now() - lastFinger >= IDLE_RELOAD_MS;
    if (!idle || busy()) { setTimeout(go, 250); return; }
    sset("k1000v2.reload", tag);
    location.reload();
  };
  go();
}

// say who is connecting (docs/14 §4.10): on each hello, and again when the APK's handshake comes
// after it (it often does: the app posts at onPageFinished). `app` is the APK's versionCode
// (`native.code`, app 0.2.2 on: android/BRIDGE.md), null in Chrome and from an older app. The box
// keeps the last `hi` of a connection.
let hiKey = null;                 // what this connection's last hi said (null: none yet)
function sayHi() {
  const caps = UI.native ? UI.native.caps.filter((c) => c.length <= 24).slice(0, 16) : [];
  const code = UI.native && Number.isInteger(UI.native.code) && UI.native.code >= 0 ? UI.native.code : null;
  const bridge = UI.native ? (Number.isInteger(UI.native.v) ? UI.native.v : 1) : null;
  const key = JSON.stringify([code, bridge, caps]);
  if (key === hiKey) return;
  hiKey = key;
  request({ t: "hi", app: code, bridge, caps }, (ok, r) => { if (ok && r.v) K.appCheck = r.v; });
}
on("hello", () => { hiKey = null; sayHi(); });
on("lost", () => { hiKey = null; });
onReady(() => { if (open && K.hello && hiKey !== null) sayHi(); });
