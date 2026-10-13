// SPDX-License-Identifier: GPL-3.0-or-later
/* Value tiles: one component for the EQ tiles, BAL, DELAY, LO-CUT, MASTER and the SETUP numbers
   (DESIGN §5.3). LIVE is touch-action:none with no scroll container, so a slide is never a scroll.

   finger                                               result
   10 px sideways (|dx| >= 2|dy|) before 14 px down     SLIDE: axis locked, captured; the first
                                                        10 px do not count
   14 px vertical first                                 nothing until it lifts (sheets: a scroll)
   lifts under 10 px of travel, within 450 ms           TAP: this field's steps open / close
   held still 0.8 s, locked tile (DELAY, LO-CUT)        `unlock` (the box opens the window)
   starts within 24 px of a side edge                   never slides (Android's Back zone)
   a second finger                                      sending stops; what was sent stays
   Every number above comes from hello.slide. */

import { K, UI, band, bandKind, canEdit, fieldOf, linked, pathOf, render, shortName, shown, slide } from "./store.js";
import { endSlide, lockedHint, onRefused, alignOpen, requestUnlock, slideDetent, stepCmd } from "./nudge.js";
import { speedMult } from "./fields.js";
import { hap } from "./haptics.js";
import { editOk, toast } from "./view.js";
import { toggleStepper, armIdle } from "./stepper.js";
import { onSecondFinger, ptrs } from "./touch.js";
import { setFill } from "./hold.js";
import { $, MINUS, bwOct, clamp, delaySub, fmtDb, fmtHz, fmtMs, fmtOct, fmtQ, h, mmss, now, num, setText } from "./util.js";

export const TILES = [];

// ------------------------------------------------------------------ what a tile shows
const BAND_TILES = new Set(["freq", "gain", "q"]);
const isBandTile = (tk) => BAND_TILES.has(tk);
export function tileLabel(el) {
  const tk = el._tk;
  if (el._label) return el._label;
  const fd = fieldOf(pathOf(tk));
  if (tk === "q") return bandKind(UI.sel) === "bell" ? "Q" : "SLOPE";
  return fd ? fd.label : ({ lowcut: "LO-CUT", bal: "BAL" })[tk] || tk.toUpperCase();
}
export const locked = (tk) => { const fd = fieldOf(pathOf(tk)); return !!(fd && fd.lock) && !alignOpen(); };
function fmtValue(tk, path, v) {
  const fd = fieldOf(path);
  if (!num(v)) return "--";
  if (tk === "freq" || (fd && fd.log && fd.unit === "Hz")) return fmtHz(v);
  if (tk === "q") return fmtQ(v);
  if (fd && fd.unit === "ms") return fmtMs(v);
  if (tk === "lowcut" || (fd && fd.unit === "Hz")) return String(Math.round(v));
  return fmtDb(v);
}
function subLine(tk, v) {
  const S = K.state, b = band(UI.sel);
  switch (tk) {
    case "freq": return shortName(UI.sel) + (b && !b.on ? " OUT · kept" : " · " + ({ bell: "bell", low_shelf: "low shelf", high_shelf: "high shelf" })[bandKind(UI.sel)]);
    case "gain": return b && !b.on ? "silent" : !num(v) ? "" : v < 0 ? "cut" : v > 0 ? "boost" : "flat";
    case "q": return bandKind(UI.sel) === "bell" ? (num(v) ? fmtOct(bwOct(v)) : "") : (b && !b.on ? "slope" : "shelf slope");
    case "bal": return !num(v) || Math.abs(v) < 0.05 ? "subs = tops" : v > 0 ? "more subs" : "more tops";
    case "delay": return delaySub(v, true);
    case "lowcut": return S && S.lowcut_on === false ? "SETUP › TOPS" : "tops 12 dB/oct";
    default: return "";
  }
}

export function makeTile(tk, opts = {}) {
  const el = h("div", "tile");
  el._tk = tk;
  el._sheet = !!opts.sheet;
  el._label = opts.label || null;
  el.dataset.f = tk;
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  el.innerHTML = '<div class="tl"><b></b><span></span></div><div class="tv"><span></span><small></small></div><div class="ts"></div><div class="rail"><i></i></div><b class="fill"></b>';
  el._l = el.querySelector(".tl b"); el._u = el.querySelector(".tl span");
  el._v = el.querySelector(".tv span"); el._vu = el.querySelector(".tv small"); el._s = el.querySelector(".ts");
  el._rail = el.querySelector(".rail"); el._mark = el.querySelector(".rail i");
  el.addEventListener("pointerdown", (e) => tileDown(e, el));
  el.addEventListener("contextmenu", (e) => e.preventDefault());
  el.addEventListener("keydown", (e) => tileKey(e, el));
  TILES.push(el);
  renderTile(el);
  return el;
}
export function forgetSheetTiles() { for (let i = TILES.length - 1; i >= 0; i--) if (TILES[i]._sheet) TILES.splice(i, 1); }

export function renderTile(el) {
  const tk = el._tk, path = pathOf(tk), fd = fieldOf(path);
  const lk = locked(tk);
  setText(el._l, tileLabel(el));
  let unit = tk === "q" ? (bandKind(UI.sel) === "bell" ? "width" : "shelf") : fd ? fd.unit : "";
  if (fd && fd.lock) unit = UI.unlockPending ? "…" : lk ? "lock" : mmss((UI.unlockUntil - now()) / 1000);
  setText(el._u, unit);
  const v = shown(path);
  let txt = fmtValue(tk, path, v), small = "";
  if (tk === "lowcut" && K.state && K.state.lowcut_on === false) txt = "OFF";
  else if (fd && (fd.unit === "ms" || (tk === "lowcut"))) small = fd.unit;
  setText(el._v, txt);
  setText(el._vu, small);
  const b = isBandTile(tk) ? band(UI.sel) : null;
  const out = !!b && !b.on;
  const sub = subLine(tk, v);
  if (out && tk !== "freq") {
    if (el._s._out !== true) { el._s.textContent = ""; el._s.append(h("span", "otag", "OUT"), document.createTextNode("")); el._s._out = true; }
    setText(el._s.lastChild, " " + sub);
  } else {
    if (el._s._out) { el._s.textContent = ""; el._s._out = false; }
    setText(el._s, sub);
  }
  el.classList.toggle("out", out);
  el.classList.toggle("focus", UI.field === tk && UI.host === el);
  el.classList.toggle("locked", !!(fd && fd.lock) && lk);
  el.classList.toggle("unlocked", !!(fd && fd.lock) && !lk);
  el.setAttribute("aria-label", (out ? shortName(UI.sel) + " out, " : "") + tileLabel(el) + " " + txt + " " + (fd ? fd.unit : "") +
    (fd && fd.lock ? (lk ? ", locked" : ", unlocked") : ""));
}
export function renderTiles() { for (const t of TILES) renderTile(t); }
export function flashTile(el, cls, ms) {
  if (!el) return;
  el.classList.add(cls);
  clearTimeout(el["_t" + cls]);
  el["_t" + cls] = setTimeout(() => el.classList.remove(cls), ms || 70);
}
onRefused((path) => { for (const t of TILES) if (pathOf(t._tk) === path) flashTile(t, "clamp", 300); });

// ------------------------------------------------------------------ the HUD (overlay strip band)
let hudT = 0;
export function hudShow(tk, mode, sliding) {
  const hud = $("#hud");
  if (!hud) return;
  const path = pathOf(tk), v = shown(path), b = band(UI.sel);
  let lab = (isBandTile(tk) ? shortName(UI.sel) + (b && !b.on ? " OUT" : "") + " · " : "") + tileLabel({ _tk: tk });
  let val = fmtValue(tk, path, v), unit = tk === "q" ? "" : (fieldOf(path) || {}).unit || "", sub = mode || "";
  if (tk === "delay") {
    lab = "DELAY" + (!num(v) || Math.abs(v) < 1e-6 ? " · ALIGNED" : v > 0 ? " · SUBS LATER" : " · TOPS LATER");
    const fs = (K.hello && K.hello.fs) || 48000;
    if (num(v)) {
      if (mode === "fine") val = (v > 0 ? "+" : v < 0 ? MINUS : "") + Math.abs(v).toFixed(3);
      sub = (Math.abs(v) * 0.343).toFixed(2) + " m · " + Math.round(Math.abs(v) * fs / 1000) + " samples" + (mode ? " · " + mode : "");
    }
  }
  if (tk === "q" && bandKind(UI.sel) === "bell" && num(v)) sub = fmtOct(bwOct(v)) + (mode ? " · " + mode : "");
  if (tk === "bal" && num(v)) sub = "subs " + fmtDb(v / 2) + " · tops " + fmtDb(-v / 2) + (mode ? " · " + mode : "");
  if (tk === "lowcut" && K.state && K.state.lowcut_on === false) sub = "OFF · SETUP › TOPS";
  setText(hud.querySelector("b"), lab);
  setText(hud.querySelector(".hv span"), val);
  setText(hud.querySelector(".hv small"), unit);
  const em = hud.querySelector("em");
  setText(em, sub);
  em.hidden = !sub;
  hud.hidden = false;
  clearTimeout(hudT);
  if (!sliding) hudT = setTimeout(() => { hud.hidden = true; }, 900);
}
export function hudHide(ms) { clearTimeout(hudT); hudT = setTimeout(() => { const hd = $("#hud"); if (hd) hd.hidden = true; }, ms || 0); }

// ------------------------------------------------------------------ the gesture
let G = null;
export const tileGesture = () => G;
function tileDown(e, el) {
  if (e.button > 0 || ptrs.size > 1 || G) return;
  const sl = slide();
  G = {
    el, tk: el._tk, pid: e.pointerId, x0: e.clientX, y0: e.clientY, t0: now(), st: "pending", maxD: 0,
    edge: e.clientX < sl.edge_px || e.clientX > window.innerWidth - sl.edge_px,
    lastX: e.clientX, acc: 0, dir: 0, rev: 0, sticky: 0, samples: [], railX: 0, start: null, mode: "", holdT: 0, held: false,
  };
  if (locked(el._tk) && linked()) {
    G.holdT = setTimeout(holdFire, sl.unlock_hold_ms);
    requestAnimationFrame(holdAnim);
  }
}
function holdAnim() {
  if (!G || !G.holdT) return;
  setFill(G.el, (now() - G.t0 - 120) / (slide().unlock_hold_ms - 120));
  requestAnimationFrame(holdAnim);
}
function holdFire() {
  const g = G;
  if (!g) return;
  g.holdT = 0; g.held = true;
  setFill(g.el, 0);
  if (g.tk === "lowcut" && K.state && K.state.lowcut_on === false) { lockedHint("lowcut_hz"); return; }
  requestUnlock();
}
function cancelHold(g) { if (g && g.holdT) { clearTimeout(g.holdT); g.holdT = 0; setFill(g.el, 0); } }

function tileMove(e) {
  const g = G, sl = slide();
  const dx = e.clientX - g.x0, dy = e.clientY - g.y0;
  g.maxD = Math.max(g.maxD, Math.hypot(dx, dy));
  if (g.st === "pending") {
    if (g.maxD >= sl.tap_px) cancelHold(g);
    if (g.held) return;
    if (Math.abs(dx) >= sl.lockin_px && Math.abs(dx) >= sl.axis_ratio * Math.abs(dy)) {
      if (g.edge) { g.st = "dead"; return; }
      if (!editOk()) { g.st = "dead"; return; }
      if (locked(g.tk)) { g.st = "dead"; lockedHint(pathOf(g.tk)); return; }
      g.st = "slide";
      g.path = pathOf(g.tk);
      g.lastX = e.clientX;
      g.start = shown(g.path);
      g.samples = [[now(), e.clientX]];
      try { g.el.setPointerCapture(g.pid); } catch (err) { /* fine */ }
      g.el.classList.add("sliding");
      railPitch(g, Math.sign(dx));
      if (!g.el._sheet) hudShow(g.tk, "", true);
      if (UI.field) armIdle();
      return;
    }
    if (Math.abs(dy) >= sl.vertical_px) g.st = "vert";
    return;
  }
  if (g.st !== "slide") return;
  const t = now(), x = e.clientX, raw = x - g.lastX;
  g.lastX = x;
  g.samples.push([t, x]);
  // the speed window: the last speed_window_ms, anchored on the newest sample at or before its
  // start (so it spans the whole window even when moves come sparse)
  while (g.samples.length > 2 && t - g.samples[1][0] >= sl.speed_window_ms) g.samples.shift();
  const fd = fieldOf(g.path);
  const master = g.path === "master_db";
  const sens = Number(UI.prefs.sens) || 1;
  const d = master && raw > 0 ? raw : raw * sens;          // master up: 20 px a detent whatever the sensitivity
  g.railX += raw;
  const dir = Math.sign(d);
  if (dir && g.dir && dir !== g.dir) { g.rev = t + sl.reverse_fine_ms; g.acc = clamp(g.acc, -sl.hysteresis_px, sl.hysteresis_px); }
  if (dir && dir !== g.dir) railPitch(g, dir);
  if (dir) g.dir = dir;
  g.acc += d;
  for (let guard = 0; guard < 60; guard++) {
    const s = Math.sign(g.acc);
    if (!s) break;
    const p = master && s > 0 ? (fd && fd.up_px_per_detent) || sl.master_up_px_per_detent : sl.px_per_detent;
    if (Math.abs(g.acc) < p) break;
    g.acc -= s * p;
    detent(g, s, fd, master);
    if (G !== g) return;
  }
  const half = g.el._rail.clientWidth / 2;
  g.el._rail.style.backgroundPositionX = (half + g.railX).toFixed(1) + "px";
  g.el._mark.style.transform = "translateX(" + clamp(g.railX, -half, half).toFixed(1) + "px)";
}
// the rail's ticks pass at the detent pitch: 14 px / sensitivity, 20 px for master up
function railPitch(g, dir) {
  const sl = slide();
  const px = g.path === "master_db" && dir > 0 ? sl.master_up_px_per_detent : sl.px_per_detent / (Number(UI.prefs.sens) || 1);
  g.el._rail.style.backgroundSize = px.toFixed(1) + "px 100%";
}
function speed(g) {
  const s = g.samples;
  if (s.length < 2) return 0;
  const dt = (s[s.length - 1][0] - s[0][0]) / 1000;
  if (dt <= 0.008) return slide().accel_from_px_s;          // too short to tell: one normal detent
  return Math.abs(s[s.length - 1][1] - s[0][1]) / dt;
}
function detent(g, s, fd, master) {
  if (!linked()) { stopGesture(); return; }           // (the link loss says it)
  let mult = 1, fine = false;
  if (master && s > 0) { g.mode = ""; }                      // never accelerated, never fine
  else {
    const sl = slide(), cap = fd && fd.accel ? (s > 0 ? fd.accel.up : fd.accel.down) : 1;
    const m = now() < g.rev ? 0 : speedMult(speed(g) * (Number(UI.prefs.sens) || 1), sl, cap);
    fine = m === 0; mult = fine ? 1 : m;
    g.mode = fd && fd.fine === fd.step && !(mult > 1) ? "" : fine ? "fine" : mult > 1 ? "×" + mult : "normal";
  }
  const ev = slideDetent(g.path, s, mult, fine, g);
  if (ev === "off") { stopGesture(); if (linked()) editOk(); return; }   // a stalled link: said
  if (ev === "locked") { stopGesture(); lockedHint(g.path); return; }
  if (ev === "swallow") { hap("click"); flashTile(g.el, "det"); return; }
  if (ev === "reject") { hap("reject"); flashTile(g.el, "clamp", 300); return; }
  if (ev === "slow") { flashTile(g.el, "clamp", 300); return; }
  hap(ev);
  flashTile(g.el, "det");
  renderTile(g.el);
  if (!g.el._sheet) hudShow(g.tk, g.mode, true);
  if (UI.field) armIdle();
  render();
}
function tileUp(cancel, e) {
  const g = G;
  G = null;
  cancelHold(g);
  if (e && e.clientX != null) g.maxD = Math.max(g.maxD, Math.hypot(e.clientX - g.x0, e.clientY - g.y0));
  if (g.st === "slide") {
    endSlide(g.path, canEdit());                       // flushed only into a live, unstalled link
    g.el.classList.remove("sliding");
    g.el._rail.style.backgroundPositionX = "";
    g.el._rail.style.backgroundSize = "";
    g.el._mark.style.transform = "";
    hudHide(700);
    render();
    return;
  }
  // a tap: still (under tap_px of travel), not an edge press that moved, not a hold
  const sl = slide();
  if (cancel || g.held || g.st !== "pending" || g.maxD >= sl.tap_px) return;
  if (locked(g.tk) && linked()) { lockedHint(pathOf(g.tk)); return; }    // a still press on a lock, let go early
  if (now() - g.t0 >= sl.tap_ms) return;
  if (!linked()) { toast("NO LINK · nothing is sent", "red"); return; }
  toggleStepper(g.tk, g.el);
}
// The slide stops here (a second finger, a link loss): what was sent stays, the rest is dropped.
export function stopGesture() {
  if (!G) return;
  const g = G;
  if (g.st === "slide") { G = null; cancelHold(g); endSlide(g.path, false); g.el.classList.remove("sliding"); hudHide(300); render(); return; }
  tileUp(true);
}
onSecondFinger(stopGesture);
window.addEventListener("pointermove", (e) => { if (G && e.pointerId === G.pid) tileMove(e); }, { passive: true });
window.addEventListener("pointerup", (e) => { if (G && e.pointerId === G.pid) tileUp(false, e); });
window.addEventListener("pointercancel", (e) => { if (G && e.pointerId === G.pid) tileUp(true); });

function tileKey(e, el) {
  const tk = el._tk;
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    if (locked(tk)) lockedHint(pathOf(tk)); else toggleStepper(tk, el);
  } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
    e.preventDefault();
    if (locked(tk)) { lockedHint(pathOf(tk)); return; }
    if (stepCmd(pathOf(tk), e.key === "ArrowRight" ? 1 : -1) && !el._sheet) hudShow(tk, "");
    render();
  }
}
