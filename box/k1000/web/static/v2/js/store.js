// SPDX-License-Identifier: GPL-3.0-or-later
/* What the page knows: the box's hello / state / meters / spectrum frame (the box is the
   truth), this phone's own UI state (selection, open stepper, sheet, prefs), and the local values
   a gesture shows before the box acks them (DESIGN §9.8: the box's value always wins; the local
   value holds only during this phone's own gesture, plus slide.echo_ms). */

import { generic } from "./fields.js";
import { load, now, num, save } from "./util.js";

export const K = {
  hello: null, state: null, meters: null, frame: null,
  link: "connecting",       // ok | connecting (first connect only) | nolink | off | busy | old
  fresh: false,             // a state arrived on THIS connection (the old one's is shown, dimmed)
  rtt: null,                // ms, from ping/pong
  heard: 0,                 // Date.now() of the last frame from the box (link.js)
};

// rta: what LIVE's graph shows (PGM ▾: box | mic | both | off); mic: SETUP › THIS PHONE › Phone
// mic ON / OFF (used only once the mic is allowed); mspeed / msmooth: the mic line's FAST / SLOW
// and smoothing (1/12, 1/6, 1/3 oct: 12 | 6 | 3); mview: MEASURE's RTA | RESPONSE; avg: 10 | 20 s
const PREF_DEFAULTS = { side: "right", haptics: "full", sens: 1, rta: "box", mic: "on", mspeed: "slow", msmooth: 6, mview: "rta", avg: 20 };
export const UI = {
  sel: null,                // the selected live band: protocol index s (LIVE_SLOTS order); per phone
  field: null, host: null,  // the open stepper: tile key, and the tile it belongs to
  unlockUntil: 0,           // ALIGN: until when the box granted this connection's unlock (ms, now())
  unlockPending: false,
  sheet: null, tab: null, pick: null, popOpen: false, dragging: false, freeze: false,
  measure: false,           // MEASURE mode (≋): the ALIGN row is the MEASURE row (measure.js)
  prefs: Object.assign({}, PREF_DEFAULTS, load("k1000v2.prefs", {})),
  native: null,             // the APK bridge's handshake (native.js), null in a browser
  diag: null,               // the APK's diagnostics, when read
};
export function savePrefs() { save("k1000v2.prefs", UI.prefs); }

// ------------------------------------------------------------------ change notification
let renderFn = () => {};
let scheduled = false;
export function onRender(fn) { renderFn = fn; }
export function render() {
  if (scheduled) return;
  scheduled = true;
  const run = () => { scheduled = false; renderFn(); };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(run); else setTimeout(run, 0);
}
export function renderNow() { scheduled = false; renderFn(); }

// ------------------------------------------------------------------ the link, as the controls see it
export const linked = () => K.link === "ok" && K.fresh && !!K.hello && !!K.state;
// A stalled link: open, but nothing heard for SLOW_MS (the box sends meters several times a
// second and the page pings every second). TCP would hold an edit sent now and deliver it late,
// in a burst with the slide the operator makes when nothing seems to happen: so edits are
// refused and said (view.editOk) until the box is heard again. MUTE still goes out (a late mute
// is the safe direction); the link is declared dead after link.js's DEAD_MS as before.
export const SLOW_MS = 2500;
export const linkSlow = () => linked() && Date.now() - K.heard > SLOW_MS;
export const canEdit = () => linked() && !linkSlow();

// ------------------------------------------------------------------ what the box sends, checked
// A hello or state of an unexpected shape never breaks the page (MUTE above all): the lists and
// maps the page walks are made what they should be, a field of the wrong type is dropped.
const isObj = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const strs = (v) => (Array.isArray(v) ? v.map((x) => (typeof x === "string" ? x : String(x))) : []);
export function cleanHello(m) {
  const H = Object.assign({}, m);
  H.bands = strs(m.bands);
  H.short = strs(m.short);
  H.kind = strs(m.kind);
  H.order = Array.isArray(m.order) && m.order.every((i) => Number.isInteger(i) && i >= 0 && i < H.bands.length) ? m.order.slice() : null;
  H.fields = isObj(m.fields) ? m.fields : {};
  H.nudge = isObj(m.nudge) ? m.nudge : {};
  H.slide = isObj(m.slide) ? m.slide : null;
  H.limits = isObj(m.limits) ? m.limits : {};
  H.surface = isObj(m.surface) ? m.surface : {};
  H.rta = isObj(m.rta) ? m.rta : {};
  H.pgm = isObj(m.pgm) ? m.pgm : null;      // RESPONSE's program windows; null = a box without them
  H.pgmKnown = "pgm" in m;                  // the key there (null = this box cannot serve them) or not (an older box)
  H.setup = (Array.isArray(m.setup) ? m.setup : []).filter((g) => isObj(g) && g.group != null)
    .map((g) => Object.assign({}, g, { group: String(g.group), items: (Array.isArray(g.items) ? g.items : []).filter(isObj) }));
  return H;
}
export function cleanState(m) {
  const S = Object.assign({}, m);
  S.bands = (Array.isArray(m.bands) ? m.bands : []).map((b) => (isObj(b) ? b : {}));
  S.setup = isObj(m.setup) ? m.setup : {};
  S.readonly = isObj(m.readonly) ? m.readonly : {};
  S.alerts = Array.isArray(m.alerts) ? m.alerts : [];
  S.pills = Array.isArray(m.pills) ? m.pills : [];
  if (!isObj(m.spectrum)) S.spectrum = null;
  if (!isObj(m.laptop)) S.laptop = null;
  if (!isObj(m.mute_band)) S.mute_band = null;
  return S;
}

// ------------------------------------------------------------------ the box's schema
// the first paint only, before the hello: the box's LIVE_SLOTS (the hello's own lists win)
const SHORT0 = ["P1", "P2", "P3", "P4", "LS", "HS"], ORDER0 = [4, 0, 1, 2, 3, 5];
export const nBands = () => (K.hello && Array.isArray(K.hello.bands) ? K.hello.bands.length : SHORT0.length);
export function order() {
  const o = K.hello && Array.isArray(K.hello.order) ? K.hello.order : K.hello ? null : ORDER0;
  return o && o.length === nBands() ? o : Array.from({ length: nBands() }, (_, i) => i);
}
export function shortName(s) { return (K.hello && K.hello.short && K.hello.short[s]) || (!K.hello && SHORT0[s]) || "B" + (s + 1); }
export function dotName(s) { const n = shortName(s); return /^P\d$/.test(n) ? n.slice(1) : n; }
export function bandKind(s) { return (K.hello && K.hello.kind && K.hello.kind[s]) || "bell"; }
export const isBell = (s) => bandKind(s) === "bell";
export function bandPath(s, p) { return (K.hello && K.hello.bands ? K.hello.bands[s] : "live_peq." + s) + "." + p; }
export function fieldOf(path) { return K.hello && K.hello.fields ? K.hello.fields[generic(path)] : null; }
export function caps(path) { const n = K.hello && K.hello.nudge; return n && n[path] ? n[path] : null; }
// hello.slide, key by key over the box's defaults (a missing or non-numeric key never makes a
// detent of 0 px)
let slideMemo = { src: undefined, v: null };
export function slide() {
  const src = K.hello ? K.hello.slide : null;
  if (slideMemo.src !== src) {
    const v = Object.assign({}, SLIDE_DEFAULT);
    if (src) for (const k of Object.keys(SLIDE_DEFAULT)) if (num(src[k]) && src[k] >= 0) v[k] = src[k];
    for (const k of ["px_per_detent", "master_up_px_per_detent", "send_ms"]) if (!(v[k] > 0)) v[k] = SLIDE_DEFAULT[k];
    slideMemo = { src, v };
  }
  return slideMemo.v;
}
// only for the first paint, before the hello: the box's own defaults (docs/14 §3.1.2)
const SLIDE_DEFAULT = { px_per_detent: 14, hysteresis_px: 4, lockin_px: 10, axis_ratio: 2, vertical_px: 14,
  tap_px: 10, tap_ms: 450, edge_px: 24, speed_window_ms: 100, fine_below_px_s: 100, accel_from_px_s: 168,
  accel_full_px_s: 560, reverse_fine_ms: 150, send_ms: 50, max_per_s: 20, echo_ms: 450,
  master_up_px_per_detent: 20, unlock_hold_ms: 800, stepper_idle_s: 8 };

// A tile key -> the nudge path it edits now (the EQ tiles follow the selected band).
export function pathOf(tk) {
  switch (tk) {
    case "freq": case "gain": case "q": return bandPath(UI.sel == null ? 0 : UI.sel, tk);
    case "master": return "master_db";
    case "bal": return "balance_db";
    case "delay": return "align_delay_ms";
    case "lowcut": return "lowcut_hz";
    default: return tk;              // SETUP numbers: the path itself (out_trim_db.2 ...)
  }
}

// The box's value of a path (state), or null.
export function boxValue(path) {
  const S = K.state;
  if (!S) return null;
  const m = /^(.*)\.(freq|gain|q)$/.exec(path);
  if (m && K.hello && K.hello.bands) {
    const s = K.hello.bands.indexOf(m[1]);
    const b = S.bands && S.bands[s];
    if (!b) return null;
    return m[2] === "freq" ? b.f : m[2] === "gain" ? b.g : b.q;
  }
  if (path in S && path !== "setup") return S[path];
  if (S.setup && path in S.setup) return S.setup[path];
  return null;
}

// ------------------------------------------------------------------ local values (optimistic)
// path -> {v, until, last}: v includes every detent sent or waiting; `last` = the id of the last
// nudge sent for it (only that ack may replace v).
export const local = new Map();
export function shown(path) {
  const p = local.get(path);
  if (p && (p.hold || now() < p.until)) return p.v;
  return boxValue(path);
}
export function setLocal(path, v, holdMs) {
  let p = local.get(path);
  if (!p) { p = { v, until: 0, last: null, hold: false }; local.set(path, p); }
  p.v = v;
  p.until = Math.max(p.until, now() + (holdMs == null ? slide().echo_ms : holdMs));
  return p;
}
export function dropLocal(path) { if (path == null) local.clear(); else local.delete(path); }

// A band as drawn: f, g, q (local while this phone edits it), `on` always the box's (D-014).
export function band(s) {
  const b = K.state && K.state.bands && K.state.bands[s];
  if (!b) return null;
  const f = shown(bandPath(s, "freq")), g = shown(bandPath(s, "gain")), q = shown(bandPath(s, "q"));
  return { f: num(f) ? f : b.f, g: num(g) ? g : b.g, q: num(q) ? q : b.q, on: b.on !== false };
}
export const effGain = (b) => (b && b.on && num(b.g) ? b.g : 0);

// The mute: what a hold means now (v1 holdUnmutes, B4's rule)
export function holdUnmutes() {
  const S = K.state;
  return !!S && (S.hold ? S.hold === "unmute" : S.muted === true);
}
export function fallback() {
  return (K.state && K.state.mute_fallback) || (K.hello && K.hello.surface && K.hello.surface.knobs === false
    ? "turn the source down · bypass" : "use B4 on the box");
}
export function alignUnlocked() { return linked() && now() < UI.unlockUntil; }
