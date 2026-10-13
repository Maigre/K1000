// SPDX-License-Identifier: GPL-3.0-or-later
/* The APK bridge (android/BRIDGE.md): a WebMessagePort the app hands over in a window `message`
   {t:"native"}, posted to the box's origin only. The page answers `ready` on every handshake
   (the app re-posts after 1.5 s and 4.5 s on a new channel until it does) and keeps the newest
   port. In Chrome no handshake ever comes: every function here is then a no-op, and haptics
   fall back to navigator.vibrate (haptics.js). `caps` is the truth; the user agent is not. */

import { UI, render } from "./store.js";

const BRIDGE_V = 1;
let port = null;
let backHandler = () => false;
let onDiag = () => {};
let insets = { top: 0, right: 0, bottom: 0, left: 0 };
let lastRects = "";
let readyHooks = [];
export const refused = [];          // origins of handshakes this page refused (the lab, Friday's check)

export const hasNative = () => !!port && !!UI.native;
export const can = (cap) => hasNative() && Array.isArray(UI.native.caps) && UI.native.caps.includes(cap);
export function nativeSend(msg) {
  if (!port) return false;
  try { port.postMessage(JSON.stringify(msg)); return true; } catch (e) { return false; }
}
export function setBackHandler(fn) { backHandler = fn; }
export function setDiagHandler(fn) { onDiag = fn; }
export function onReady(fn) { readyHooks.push(fn); }
export const nativeInsets = () => insets;

function onApp(m) {
  if (!m || typeof m !== "object") return;
  switch (m.t) {
    case "back": {
      // answer at once (the app waits 400 ms), never twice
      let handled = false;
      try { handled = !!backHandler(); } catch (e) { handled = false; }
      nativeSend({ t: "back_handled", id: m.id, handled });
      break;
    }
    case "insets":
      insets = { top: +m.top || 0, right: +m.right || 0, bottom: +m.bottom || 0, left: +m.left || 0 };
      try {
        const st = document.documentElement.style;
        for (const k of Object.keys(insets)) st.setProperty("--in-" + k, insets[k] + "px");
      } catch (e) { /* fine */ }
      render();
      break;
    case "diag":
      UI.diag = m;
      onDiag(m);
      render();
      break;
    case "err":
      // a message the app refused: logged for the diagnostics, never shown mid-show
      UI.nativeErr = String(m.m || "") + ": " + String(m.why || "");
      break;
    default:
      break;
  }
}

// Module top level: in place long before the app's onPageFinished (BRIDGE.md, "Transport").
if (typeof window !== "undefined") {
  window.addEventListener("message", (e) => {
    if (typeof e.data !== "string" || !e.ports || e.ports.length !== 1) return;
    // the WebView's postWebMessage arrives with origin "" (no window can post that: an opaque
    // origin is "null"); a window of this origin with location.origin. Anything else (another
    // origin, a sandboxed frame or popup holding a reference to this page) never gets the port.
    let m;
    try { m = JSON.parse(e.data); } catch (err) { return; }
    if (!m || m.t !== "native") return;
    if (e.origin !== "" && e.origin !== location.origin) {
      refused.push(String(e.origin).slice(0, 60));          // shown in THIS PHONE › Diagnostics
      if (refused.length > 4) refused.shift();
      return;
    }
    if (port) { try { port.close(); } catch (err) { /* already closed */ } }
    port = e.ports[0];
    UI.native = { v: m.v, app: String(m.app || ""), build: String(m.build || ""), ui: m.ui,
      code: Number.isInteger(m.code) ? m.code : null,          // the versionCode, once the app sends it
      caps: Array.isArray(m.caps) ? m.caps.filter((c) => typeof c === "string") : [] };
    const mine = port;
    port.onmessage = (ev) => {
      if (port !== mine || typeof ev.data !== "string") return;
      let x;
      try { x = JSON.parse(ev.data); } catch (err) { return; }
      onApp(x);
    };
    nativeSend({ t: "ready", v: BRIDGE_V, caps: ["back"] });
    lastRects = "";
    for (const f of readyHooks) { try { f(); } catch (err) { /* one hook never stops the rest */ } }
    render();
  });
}

// The rows a slide happens on, kept off Android's Back gesture (BRIDGE.md, "Exclusion rects").
// rects: up to 16 [x, y, w, h] in CSS px; only sent when they changed.
export function sendExclusion(rects) {
  if (!can("exclusion")) return;
  const r = rects.slice(0, 16).map((a) => a.map((v) => Math.round(v)));
  const key = JSON.stringify(r);
  if (key === lastRects) return;
  lastRects = key;
  nativeSend({ t: "exclusion", rects: r });
}
export function requestDiag(show) { if (can("diag")) nativeSend(show ? { t: "diag", show: true } : { t: "diag" }); }
