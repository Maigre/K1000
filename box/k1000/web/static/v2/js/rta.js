// SPDX-License-Identifier: GPL-3.0-or-later
/* The box spectrum on the phone (docs/14 §3.4, §4.8). The page subscribes only while LIVE is
   visible (page shown, portrait: no rotate notice over the graph, no sheet over it) and this
   phone shows the box spectrum (the PGM ▾ popover, not FROZEN); it unsubscribes when hidden,
   and subscribes again after the box's
   `unsub` (10 s without a ping) or a reconnect. The 2 s ping keeps a subscription alive.
   Ballistics, hold dots and the PEAK are the box's: the same picture as the box screen.

   The phone mic (DESIGN §7) comes after the gig. Its hook is `mic` below: a MicSource (Chrome
   getUserMedia, or the APK's raw mic) sets mic.source = {bins: Float32Array dBFS, f0, f1, peak_hz}
   and calls render(); graph.js then draws it as the cyan line over the box bars. The mic is
   asked for only from SETUP › THIS PHONE, never from LIVE (DESIGN §9.15). */

import { K, UI, linked, render } from "./store.js";
import { on, request } from "./link.js";
import { num } from "./util.js";

export const mic = { source: null };
export const R = { frame: null, subscribed: false, asked: null, frames: 0 };

function b64bytes(s) {
  try {
    const bin = atob(s);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  } catch (e) { return null; }
}
// One frame -> {st, n, f0, f1, hi, lo, bins, hold, peak_hz, seq}; no picture: {st, n: 0}
export function decodeFrame(m, stepDb = 0.5) {
  const st = String(m.st || "off");
  const n = m.n | 0;
  const b = typeof m.b === "string" ? b64bytes(m.b) : null;
  if (st !== "live" || !b || !n || b.length < n || !num(m.f0) || !num(m.f1) || !num(m.hi)) {
    return { st, n: 0, seq: m.seq, peak_hz: null };
  }
  const bins = new Float32Array(n);
  for (let i = 0; i < n; i++) bins[i] = m.hi - b[i] * stepDb;
  let hold = null;
  const hb = typeof m.h === "string" ? b64bytes(m.h) : null;
  if (hb && hb.length >= n) { hold = new Float32Array(n); for (let i = 0; i < n; i++) hold[i] = m.hi - hb[i] * stepDb; }
  return { st, n, f0: m.f0, f1: m.f1, hi: m.hi, lo: num(m.lo) ? m.lo : m.hi - 66, bins, hold,
    peak_hz: num(m.peak_hz) && m.peak_hz > 0 ? m.peak_hz : null, seq: m.seq };
}
export const binHz = (fr, i) => fr.f0 * Math.pow(fr.f1 / fr.f0, i / (fr.n - 1));

// the rotate notice covers the graph (app.css: the same query)
const ROTATED = typeof matchMedia === "function" ? matchMedia("(orientation: landscape) and (max-height: 440px)") : null;
const rotated = () => !!ROTATED && ROTATED.matches;
if (ROTATED) {
  const re = () => syncSub();
  if (ROTATED.addEventListener) ROTATED.addEventListener("change", re); else if (ROTATED.addListener) ROTATED.addListener(re);
}

// What this phone wants now
export function wanted() {
  const H = K.hello;
  return linked() && !document.hidden && !rotated() && !UI.sheet && !UI.freeze && UI.prefs.rta === "box" &&
    !!H && !(H.rta && H.rta.enabled === false);
}
export function syncSub() {
  const want = wanted();
  if (R.asked === want && R.subscribed === want) return;
  if (!linked()) { R.asked = null; R.subscribed = false; return; }
  R.asked = want;
  R.subscribed = want;           // frames may arrive before the ack
  if (!want && !UI.freeze) { R.frame = null; render(); }
  request({ t: "sub", rta: want }, (ok, r) => {
    if (!ok && r.code !== "LINK" && R.asked === want) { R.asked = null; R.subscribed = false; }
  }, 3000);
}

on("rta", (m) => {
  if (UI.freeze) return;
  const step = K.hello && K.hello.rta && num(K.hello.rta.step_db) ? K.hello.rta.step_db : 0.5;
  R.frame = decodeFrame(m, step);
  R.frames++;
  render();
});
on("unsub", () => { R.subscribed = false; R.asked = null; syncSub(); });   // lapsed: again, if still wanted
on("lost", () => { R.subscribed = false; R.asked = null; if (!UI.freeze) R.frame = null; });
on("state", () => syncSub());
on("visibility", () => syncSub());

// The nearest detected peak within ±⅓ oct of `f` on the box picture (a long-press snaps to it):
// a local maximum standing 3 dB over the bins 3 away on each side. Else `f` itself.
export function snapToPeak(f) {
  const fr = R.frame;
  if (!fr || !fr.n) return f;
  const arr = fr.bins;
  let best = 0, bd = 1 / 3;
  for (let i = 3; i < fr.n - 3; i++) {
    if (arr[i] < arr[i - 1] || arr[i] < arr[i + 1]) continue;
    if (arr[i] - (arr[i - 3] + arr[i + 3]) / 2 < 3) continue;
    const hz = binHz(fr, i), d = Math.abs(Math.log2(hz / f));
    if (d <= bd) { bd = d; best = hz; }
  }
  return best || f;
}
export const peakHz = () => (R.frame && R.frame.st === "live" ? R.frame.peak_hz : null);
export const boxLive = () => !!R.frame && R.frame.st === "live" && R.frame.n > 0;
