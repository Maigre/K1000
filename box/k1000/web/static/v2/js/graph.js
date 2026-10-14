// SPDX-License-Identifier: GPL-3.0-or-later
/* The graph (DESIGN §5.6, §7): the box spectrum (teal bars and hold dots, the box's own
   ballistics), the phone mic's cyan line (mic.js) or MEASURE's RESPONSE (measure.js), the PEAK
   of the source shown (the mic's own when its line is on the graph, else the box's), the EQ
   curve on top (OUT bands left out, a faint dashed ghost of what they would add) and the band
   dots, all over a fixed +7 / −21 dB scale; y 0-44 is the overlay strip (PGM ▾, pills, ▼ PEAK,
   ≋ fixed at the right edge so it never moves when the PEAK comes or goes, the HUD), above
   +7 dB where no dot can be.

   gesture                       result
   tap a dot                     selects it; changes nothing
   drag a dot                    `band {s, f, g}` coalesced at 50 ms (the coarse tool): grab within
                                 28 px of the selected dot or an active one, 12 px deadband per
                                 axis, gain on a 0.5 dB grid with a 0 dB magnet, freq on 1/12 oct
   long-press 550 ms empty graph `grab {hz, s}`: the box's B3 helper at the nearest peak within
                                 ±⅓ oct (else the finger's frequency), silent; then the GAIN steps
   long-press ▼ PEAK / marker    `grab {}`: the helper at the box's PEAK, exactly B3; on the mic's
                                 PEAK `grab {hz}` (the box cannot see it): the same silent helper.
                                 The PEAK is the one under the finger at TOUCH-DOWN: a mic PEAK
                                 that goes during the hold (the music drops) still sends its own
                                 hz, never the box's `grab {}`
   tap empty graph               closes the steps / the popover
   a second finger               stops it all: a drag sends nothing more (not even its last
                                 value; what was sent stays), a pending long-press is dropped */

import { K, UI, band, bandKind, bandPath, canEdit, dotName, effGain, fieldOf, linked, nBands, render, savePrefs, setLocal, local, shortName, slide } from "./store.js";
import { R, activePeak, boxLive, boxShown, binHz, mic, resp, snapToPeak, syncSub } from "./rta.js";
import { M as MIC, allowed as micAllowed, clearErr as micClearErr, frac as micFrac, sync as micSync } from "./mic.js";
import { toggleMeasure } from "./measure.js";
import { request, send } from "./link.js";
import { hap } from "./haptics.js";
import { editOk, toast } from "./view.js";
import { bindHold } from "./hold.js";
import { closeStepper, openStepper } from "./stepper.js";
import { selectBand } from "./bands.js";
import { onSecondFinger, ptrs } from "./touch.js";
import { $, $$, clamp, fmtDb, fmtHz, now, num, setText } from "./util.js";

const FS_DEFAULT = 48000;
const PAD_L = 24, PAD_R = 4, Y_TOP = 44, DB_TOP = 7, DB_BOT = -21;
let cv, cx, graphEl, C = {};
let GW = 300, GH = 300, DPR = 1, XS = [], FX = [], xsN = 0;
let dirty = true;
export const markDirty = () => { dirty = true; };
const yBot = () => GH - 16;
const xOf = (f) => PAD_L + Math.log10(f / 20) / 3 * (GW - PAD_L - PAD_R);
const fOf = (x) => 20 * Math.pow(10, 3 * (x - PAD_L) / (GW - PAD_L - PAD_R));
const yOf = (db) => Y_TOP + (DB_TOP - db) / (DB_TOP - DB_BOT) * (yBot() - Y_TOP);
const pxPerOct = () => (GW - PAD_L - PAD_R) / (3 * Math.log2(10));
const pxPerDb = () => (yBot() - Y_TOP) / (DB_TOP - DB_BOT);
let notchFlash = null;

// ------------------------------------------------------------------ biquads (RBJ cookbook, as CamillaDSP)
const FS = () => (K.hello && num(K.hello.fs) ? K.hello.fs : FS_DEFAULT);
function coef(kind, f, g, q) {
  const w0 = 2 * Math.PI * f / FS(), cw = Math.cos(w0), sw = Math.sin(w0), A = Math.pow(10, g / 40), al = sw / (2 * q);
  if (kind === "bell") return [1 + al * A, -2 * cw, 1 - al * A, 1 + al / A, -2 * cw, 1 - al / A];
  const sA = 2 * Math.sqrt(A) * al;
  if (kind === "low_shelf") return [A * ((A + 1) - (A - 1) * cw + sA), 2 * A * ((A - 1) - (A + 1) * cw), A * ((A + 1) - (A - 1) * cw - sA),
    (A + 1) + (A - 1) * cw + sA, -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sA];
  if (kind === "high_shelf") return [A * ((A + 1) + (A - 1) * cw + sA), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sA),
    (A + 1) - (A - 1) * cw + sA, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sA];
  return [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + al, -2 * cw, 1 - al];     // high-pass (the Lo-Cut, Q 0.707)
}
function magDb(c, f) {
  const w = 2 * Math.PI * f / FS(), c1 = Math.cos(w), s1 = Math.sin(w), c2 = Math.cos(2 * w), s2 = Math.sin(2 * w);
  const nr = c[0] + c[1] * c1 + c[2] * c2, ni = -(c[1] * s1 + c[2] * s2);
  const dr = c[3] + c[4] * c1 + c[5] * c2, di = -(c[4] * s1 + c[5] * s2);
  return 10 * Math.log10((nr * nr + ni * ni) / (dr * dr + di * di));
}
const cache = new Map();
function bandArr(s, b) {
  const key = bandKind(s) + "|" + b.f + "|" + b.g + "|" + b.q + "|" + xsN;
  const c = cache.get(s);
  if (c && c.key === key) return c.arr;
  const arr = new Float32Array(xsN);
  if (b.g !== 0 && num(b.f) && num(b.q)) { const co = coef(bandKind(s), b.f, b.g, b.q); for (let k = 0; k < xsN; k++) arr[k] = magDb(co, FX[k]); }
  cache.set(s, { key, arr });
  return arr;
}
let hpCache = { key: "", arr: null };
function hpArr(hz) {
  const key = hz + "|" + xsN;
  if (hpCache.key !== key) {
    const co = coef("hp", hz, 0, 0.7071);
    hpCache = { key, arr: new Float32Array(xsN) };
    for (let k = 0; k < xsN; k++) hpCache.arr[k] = magDb(co, FX[k]);
  }
  return hpCache.arr;
}

// ------------------------------------------------------------------ drawing
function resize() {
  const r = graphEl.getBoundingClientRect();
  DPR = Math.min(window.devicePixelRatio || 1, 3);
  GW = Math.max(100, r.width); GH = Math.max(80, r.height);
  cv.width = Math.round(GW * DPR); cv.height = Math.round(GH * DPR);
  XS = []; FX = [];
  for (let x = PAD_L; x <= GW - PAD_R + 0.01; x += 2) { XS.push(x); FX.push(fOf(x)); }
  xsN = XS.length;
  cache.clear(); hpCache.key = "";
  dirty = true;
}
function curvePath(arr, color, width, dash, alpha) {
  const yt = Y_TOP - 8, yb = GH;
  cx.beginPath();
  for (let k = 0; k < xsN; k++) {
    const y = clamp(yOf(arr[k]), yt, yb);
    if (k) cx.lineTo(XS[k], y); else cx.moveTo(XS[k], y);
  }
  cx.globalAlpha = alpha == null ? 1 : alpha;
  cx.strokeStyle = color; cx.lineWidth = width;
  cx.setLineDash(dash || []);
  cx.stroke();
  cx.setLineDash([]);
  cx.globalAlpha = 1;
}
// a bin level (the frame's hi..lo display range) -> y: bars stand from the floor up to under 0 dB
function hS(fr, L) { return yBot() - clamp((L - fr.lo) / (fr.hi - fr.lo), 0, 1) * 0.95 * (yBot() - yOf(0)); }
function drawBars(fr) {
  const yb = yBot(), half = Math.pow(fr.f1 / fr.f0, 0.5 / (fr.n - 1));
  cx.fillStyle = C.teal;
  for (let i = 0; i < fr.n; i++) {
    const f = binHz(fr, i), x0 = xOf(f / half), x1 = xOf(f * half), y = hS(fr, fr.bins[i]);
    if (x1 < PAD_L || y >= yb) continue;
    cx.fillRect(x0, y, Math.max(0.8, x1 - x0 - 0.45), yb - y);
  }
  if (!fr.hold) return;
  cx.fillStyle = C.tealHi;
  for (let i = 0; i < fr.n; i++) {
    const f = binHz(fr, i), x0 = xOf(f / half), x1 = xOf(f * half), y = hS(fr, fr.hold[i]);
    if (x1 < PAD_L || y >= yb - 2) continue;
    cx.fillRect(x0, y - 1, Math.max(0.8, x1 - x0 - 0.45), 2);
  }
}
function drawEq() {
  if (!xsN || !K.state) return;
  const tot = new Float32Array(xsN), ghost = new Float32Array(xsN);
  let anyOut = false;
  for (let s = 0; s < nBands(); s++) {
    const b = band(s);
    if (!b) continue;
    const a = bandArr(s, b);
    if (!b.on && b.g !== 0) anyOut = true;
    for (let k = 0; k < xsN; k++) { ghost[k] += a[k]; if (b.on) tot[k] += a[k]; }
  }
  const sb = UI.sel != null ? band(UI.sel) : null;
  if (sb && sb.g !== 0) {                     // the selected band's share, filled (faint when OUT)
    const a = bandArr(UI.sel, sb), y0 = yOf(0);
    cx.beginPath(); cx.moveTo(XS[0], y0);
    for (let k = 0; k < xsN; k++) cx.lineTo(XS[k], clamp(yOf(a[k]), Y_TOP - 8, GH));
    cx.lineTo(XS[xsN - 1], y0); cx.closePath();
    cx.fillStyle = sb.on ? "rgba(255,184,0,.18)" : "rgba(255,255,255,.06)";
    cx.fill();
  }
  if (anyOut) curvePath(ghost, C.fg, 1.5, [5, 5], 0.4);
  const S = K.state;
  const lc = local.get("lowcut_hz");
  const lcHz = lc && (lc.hold || now() < lc.until) ? lc.v : S.lowcut_hz;
  if (S.lowcut_on && num(lcHz)) {              // the tops' curve with the Lo-Cut, under 1 kHz
    const hp = hpArr(lcHz), tops = new Float32Array(xsN);
    let last = 0;
    for (let k = 0; k < xsN; k++) { tops[k] = tot[k] + hp[k]; if (FX[k] < 1000) last = k; }
    cx.save();
    cx.beginPath(); cx.rect(0, 0, XS[last], GH); cx.clip();
    curvePath(tops, C.dim, 1.5, null, 0.9);
    cx.restore();
    const xl = xOf(Math.max(25, lcHz * 0.42));
    const k = clamp(Math.round((xl - PAD_L) / 2), 0, xsN - 1);
    cx.fillStyle = C.dim; cx.font = "700 9.5px system-ui, Roboto, sans-serif"; cx.textAlign = "center";
    cx.fillText("tops", xl, clamp(yOf(tops[k]) - 7, Y_TOP + 8, yBot() - 4));
  }
  curvePath(tot, C.fg, 2.5);
}
function dotXY(s) { const b = band(s); return b ? [xOf(b.f), yOf(b.g)] : [-99, -99]; }
const dotR = () => (GW < 380 ? 11 : 12);
function drawDots() {
  if (!K.state) return;
  const r = dotR();
  const all = Array.from({ length: nBands() }, (_, i) => i);
  const ord = all.filter((i) => i !== UI.sel).concat(UI.sel != null ? [UI.sel] : []);
  cx.font = "800 11px system-ui, Roboto, sans-serif"; cx.textAlign = "center"; cx.textBaseline = "middle";
  for (const s of ord) {
    const b = band(s);
    if (!b) continue;
    const [x, y0] = dotXY(s), y = clamp(y0, Y_TOP - 2, yBot());
    const col = b.g < 0 ? C.cut : b.g > 0 ? C.green : "#2a2a2a";
    const sel = s === UI.sel, rr = sel ? r + 2 : r;
    if (sel) { cx.beginPath(); cx.arc(x, y, rr + 4, 0, Math.PI * 2); cx.strokeStyle = C.amber; cx.lineWidth = 3; cx.stroke(); }
    cx.beginPath(); cx.arc(x, y, rr, 0, Math.PI * 2);
    if (b.on) {
      cx.fillStyle = col; cx.fill();
      if (b.g === 0) { cx.strokeStyle = "#6a6a6a"; cx.lineWidth = 1.5; cx.stroke(); }
      cx.fillStyle = b.g === 0 ? C.dim : C.ink;
    } else {                                   // OUT: hollow, a dashed ring in the band's colour
      cx.fillStyle = C.graph; cx.fill();
      cx.setLineDash([3, 3]); cx.strokeStyle = b.g === 0 ? C.dim : col; cx.lineWidth = 2; cx.stroke(); cx.setLineDash([]);
      cx.fillStyle = b.g === 0 ? C.dim : col;
    }
    cx.fillText(dotName(s), x, y + 0.5);
  }
  cx.textBaseline = "alphabetic";
}
function draw() {
  cx.setTransform(DPR, 0, 0, DPR, 0, 0);
  cx.fillStyle = C.graph; cx.fillRect(0, 0, GW, GH);
  const yb = yBot();
  cx.lineWidth = 1;
  for (const f of [30, 40, 50, 60, 70, 80, 90, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]) {
    const x = Math.round(xOf(f)) + 0.5;
    cx.strokeStyle = (f === 100 || f === 1000 || f === 10000) ? C.gridHi : C.grid;
    cx.beginPath(); cx.moveTo(x, Y_TOP - 4); cx.lineTo(x, yb); cx.stroke();
  }
  for (const db of [6, 0, -6, -12, -18]) {
    const y = Math.round(yOf(db)) + 0.5;
    cx.strokeStyle = db === 0 ? C.gridHi : C.grid;
    cx.beginPath(); cx.moveTo(PAD_L, y); cx.lineTo(GW, y); cx.stroke();
  }
  cx.font = "600 11px system-ui, Roboto, sans-serif";
  cx.fillStyle = C.axis; cx.textAlign = "left"; cx.textBaseline = "middle";
  for (const db of [6, 0, -6, -12, -18]) cx.fillText(db > 0 ? "+" + db : db < 0 ? "−" + (-db) : "0", 2, yOf(db));
  cx.textAlign = "center"; cx.textBaseline = "alphabetic";
  for (const [f, t] of [[50, "50"], [100, "100"], [200, "200"], [500, "500"], [1000, "1k"], [2000, "2k"], [5000, "5k"], [10000, "10k"]]) cx.fillText(t, xOf(f), GH - 4);
  cx.save();
  cx.beginPath(); cx.rect(PAD_L, 0, GW - PAD_L, yb + 1); cx.clip();
  if (!UI.dragging) {
    if (boxShown() && boxLive()) drawBars(R.frame);
    const lay = { xOf, yOf, top: Y_TOP - 8, bot: yb, cyan: C.cyan };
    if (typeof mic.draw === "function") mic.draw(cx, lay);
    if (typeof resp.draw === "function") resp.draw(cx, lay);
    const ap = activePeak(), pk = ap ? ap.hz : null;
    if (pk) {
      const x = xOf(pk);
      cx.strokeStyle = C.cyan; cx.globalAlpha = 0.55; cx.setLineDash([3, 4]); cx.lineWidth = 1.5;
      cx.beginPath(); cx.moveTo(x, Y_TOP); cx.lineTo(x, yb); cx.stroke();
      cx.setLineDash([]); cx.globalAlpha = 1;
      cx.fillStyle = C.cyan;
      cx.beginPath(); cx.moveTo(x - 6, Y_TOP); cx.lineTo(x + 6, Y_TOP); cx.lineTo(x, Y_TOP + 8); cx.closePath(); cx.fill();
    }
  }
  if (notchFlash) {
    const a = 1 - (now() - notchFlash.t) / 900;
    if (a <= 0) notchFlash = null;
    else {
      const x = xOf(notchFlash.f);
      cx.strokeStyle = C.cyan; cx.globalAlpha = a; cx.lineWidth = 3;
      cx.beginPath(); cx.moveTo(x, Y_TOP); cx.lineTo(x, yb); cx.stroke();
      cx.globalAlpha = 1;
      dirty = true;
    }
  }
  drawEq();
  cx.restore();
  drawDots();
}
function loop() {
  if (dirty && cx) { dirty = false; try { draw(); } catch (e) { setTimeout(() => { throw e; }, 0); } }
  requestAnimationFrame(loop);
}

// ------------------------------------------------------------------ gestures
let G = null;
export const graphGesture = () => G;
function cvPt(e) { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
function dragHit(p) {
  let best = -1, bd = 28;
  for (let s = 0; s < nBands(); s++) {
    const b = band(s);
    if (!b || (s !== UI.sel && effGain(b) === 0 && b.g === 0)) continue;     // a flat dot moves only while selected
    const [x, y] = dotXY(s), d = Math.hypot(p.x - x, p.y - clamp(y, Y_TOP - 2, yBot()));
    if (d < bd || (d <= 28 && s === UI.sel && d < bd + 6)) { bd = d; best = s; }
  }
  return best;
}
function tapHit(p) {
  let best = -1, bd = 24;
  for (let s = 0; s < nBands(); s++) {
    const [x, y] = dotXY(s), d = Math.hypot(p.x - x, p.y - clamp(y, Y_TOP - 2, yBot()));
    if (d < bd) { bd = d; best = s; }
  }
  return best;
}
// the PEAK marker under the finger: the PEAK itself ({hz, src}), recorded at touch-down, or null
const peakHit = (p) => { const ap = activePeak(); return ap && Math.abs(p.x - xOf(ap.hz)) <= 24 ? { hz: ap.hz, src: ap.src } : null; };
let ringRaf = 0;
function ringStart(p, t0) {
  const ring = $("#ring");
  ring.style.left = p.x + "px"; ring.style.top = p.y + "px";
  ring.hidden = false;
  const step = () => {
    const k = clamp((now() - t0) / 550, 0, 1);
    ring.style.transform = "scale(" + (0.3 + 0.7 * k).toFixed(3) + ")";
    ring.style.opacity = (0.25 + 0.75 * k).toFixed(2);
    if (k < 1 && !ring.hidden) ringRaf = requestAnimationFrame(step);
  };
  cancelAnimationFrame(ringRaf);
  ringRaf = requestAnimationFrame(step);
}
function ringStop() { cancelAnimationFrame(ringRaf); const r = $("#ring"); if (r) r.hidden = true; }

function graphDown(e) {
  if (e.button > 0 || ptrs.size > 1 || G) return;
  if (UI.popOpen) { closePop(); return; }
  const p = cvPt(e);
  const dh = linked() ? dragHit(p) : -1;
  G = { pid: e.pointerId, p0: p, t0: now(), dh, th: tapHit(p), ap: peakHit(p), st: "pending", lp: 0, fired: false, sendT: 0, last: null };
  try { cv.setPointerCapture(e.pointerId); } catch (err) { /* fine */ }
  if (dh < 0 && linked()) { G.lp = setTimeout(graphLong, 550); ringStart(p, G.t0); }
}
function graphMove(e) {
  const g = G, p = cvPt(e), dx = p.x - g.p0.x, dy = p.y - g.p0.y;
  if (g.dh >= 0) {
    if (g.st === "pending" && (Math.abs(dx) > 12 || Math.abs(dy) > 12)) {
      g.st = "drag"; UI.dragging = true;
      selectBand(g.dh);
      const b = band(g.dh);
      g.f0 = b.f; g.g0 = b.g;
      dirty = true;
    }
    if (g.st === "drag") dotDrag(g, dx, dy, p);
  } else if (g.st === "pending" && Math.hypot(dx, dy) > 10) {
    clearTimeout(g.lp); ringStop(); g.st = "dead";
  }
}
function dotDrag(g, dx, dy, p) {
  if (!canEdit()) { graphUp(true, true); if (linked()) editOk(); return; }
  const s = g.dh, b = band(s);
  const ff = fieldOf(bandPath(s, "freq")) || { min: 20, max: 20000 }, gf = fieldOf(bandPath(s, "gain")) || { min: -20, max: 6 };
  const ex = Math.abs(dx) > 12 ? dx - 12 * Math.sign(dx) : 0;
  const ey = Math.abs(dy) > 12 ? dy - 12 * Math.sign(dy) : 0;
  const nf = Math.round(clamp(g.f0 * Math.pow(2, Math.round(ex / pxPerOct() * 12) / 12), ff.min, ff.max) * 10) / 10;
  let ng = g.g0;
  if (ey) {
    ng = Math.round((g.g0 - ey / pxPerDb()) * 2) / 2;
    if (Math.abs(ng) < 0.75) ng = 0;                    // the 0 dB magnet
    ng = clamp(ng, gf.min, gf.max);
  }
  if (nf !== b.f || ng !== b.g) {
    const hit0 = ng === 0 && b.g !== 0;
    setLocal(bandPath(s, "freq"), nf).hold = true;
    setLocal(bandPath(s, "gain"), ng).hold = true;
    g.last = { s, f: nf, g: ng };
    hap(hit0 ? "click" : "tick");
    queueDrag(g);
    dirty = true;
    render();
  }
  const ro = $("#readout");
  ro.textContent = "";
  const lb = document.createElement("b");
  lb.textContent = shortName(s) + (b.on ? "" : " OUT");
  ro.append(lb, document.createTextNode(fmtHz(nf) + " · " + fmtDb(ng) + " dB"));
  ro.style.left = clamp(p.x, 80, GW - 80) + "px";
  ro.style.top = Math.max(84, p.y - 60) + "px";
  ro.hidden = false;
}
// at most one band message per slide.send_ms, the last value always sent
function queueDrag(g) {
  if (g.sendT) return;
  const go = () => {
    g.sendT = 0;
    if (!g.last || !canEdit()) return;
    const m = g.last; g.last = null;
    send({ t: "band", s: m.s, f: m.f, g: m.g });
    g.sentAt = now();
  };
  const wait = Math.max(0, (Number(slide().send_ms) || 50) - (now() - (g.sentAt || 0)));
  if (!wait) go(); else g.sendT = setTimeout(go, wait);
}
function graphLong() {
  const g = G;
  if (!g) return;
  g.fired = true; g.lp = 0;
  ringStop();
  hap("arm");
  if (g.ap) grabPeak(g.ap);
  else grab(snapToPeak(clamp(fOf(g.p0.x), 20, 20000)));
}
// The PEAK pressed (recorded at touch-down, never re-read when the hold fires: the mic PEAK may
// have gone meanwhile, and the box's PEAK is not what the finger is on): the box's = `grab {}`
// (exactly B3); the mic's = `grab {hz}` at the frequency pressed, the same silent helper
function grabPeak(ap) {
  if (ap.src === "mic") grab(ap.hz, "mic");
  else grab(null);
}
// stop: the drag ends WITHOUT sending what waits (a second finger, a stalled link)
function graphUp(cancel, stop) {
  const g = G;
  G = null;
  if (!g) return;
  clearTimeout(g.lp); ringStop();
  if (g.st === "drag") {
    clearTimeout(g.sendT);
    if (g.last && !stop && canEdit()) send({ t: "band", s: g.last.s, f: g.last.f, g: g.last.g });
    g.last = null;
    UI.dragging = false;
    $("#readout").hidden = true;
    for (const p of ["freq", "gain"]) { const l = local.get(bandPath(g.dh, p)); if (l) { l.hold = false; l.until = now() + slide().echo_ms; } }
    dirty = true; render();
    return;
  }
  if (cancel || g.st !== "pending" || g.fired) return;
  if (now() - g.t0 < 450) {
    const s = g.dh >= 0 ? g.dh : g.th;
    if (s >= 0) { selectBand(s); hap("tick"); } else closeStepper();
  }
}

// The box's notch helper (B3), silent: at `hz`, or at the box's PEAK when hz is null. src "mic":
// hz is the phone mic's PEAK (said so).
let grabbing = false;
export function grab(hz, src) {
  if (!editOk()) return;
  if (grabbing) return;
  grabbing = true;
  const msg = { t: "grab" };
  if (hz != null) msg.hz = Math.round(hz * 10) / 10;
  if (UI.sel != null) msg.s = UI.sel;
  const id = request(msg, (ok, r) => {
    grabbing = false;
    if (ok && r.v && Number.isInteger(r.v.s)) {
      const v = r.v;
      selectBand(v.s);
      notchFlash = { f: num(v.f) ? v.f : hz || 1000, t: now() };
      dirty = true;
      hap("ok");
      openStepper("gain", null);
      const at = src === "mic" ? "MIC PEAK · " : hz == null ? (v.peak ? "PEAK · " : "NO PEAK · Q only · ") : "";
      toast(at + (v.new ? shortName(v.s) + " → " + fmtHz(v.f) + " · Q " + (num(v.q) ? v.q.toFixed(2) : "--") + " · silent · now cut"
        : shortName(v.s) + " already there · selected"), "white", 2600);
    } else if (!ok && r.code === "NOFREE") { toast("NO FREE BAND · FLAT one first", "red", 2600); hap("reject"); }
    else if (!ok && r.code === "TIMEOUT") { toast("No answer from K1000 for the notch", "red", 2600); hap("fail"); }
    else if (!ok && r.code !== "LINK") { toast(String(r.why || "refused"), "red"); hap("reject"); }
    render();
  }, 1500);
  if (id == null) grabbing = false;
}

// ------------------------------------------------------------------ the overlay strip: PGM ▾, ▼ PEAK, ≋
export function openPop() { UI.popOpen = true; $("#pop").hidden = false; $("#srcBtn").setAttribute("aria-expanded", "true"); renderPop(); }
export function closePop() { if (!UI.popOpen) return false; UI.popOpen = false; $("#pop").hidden = true; $("#srcBtn").setAttribute("aria-expanded", "false"); return true; }
const SRC_WORD = { box: "PGM", mic: "MIC", both: "PGM+MIC", off: "RTA OFF" };
const micChosen = () => UI.prefs.rta === "mic" || UI.prefs.rta === "both";
function micWords() {
  if (!micAllowed()) return MIC.perm === "denied" ? "Phone mic: BLOCKED. SETUP › THIS PHONE says how to allow it again. "
    : MIC.perm === "unsupported" ? "Phone mic: " + (MIC.err || "not in this browser") + ". " : "Phone mic: ALLOW THE MIC: SETUP › THIS PHONE (asked there only, never from LIVE). ";
  if (UI.prefs.mic === "off") return "Phone mic: OFF in SETUP › THIS PHONE. ";
  return "Phone mic: the cyan line, uncalibrated, with its own PEAK" + (MIC.err ? " (" + MIC.err + ")" : "") + ". ";
}
function renderPop() {
  for (const b of $$("#popShow button")) b.setAttribute("aria-pressed", String(b.dataset.v === UI.prefs.rta));
  for (const b of $$("#popMic button")) b.setAttribute("aria-pressed", String(b.dataset.k === "mspeed" ? b.dataset.v === (UI.prefs.mspeed === "fast" ? "fast" : "slow") : +b.dataset.v === micFrac()));
  $("#popFreeze").setAttribute("aria-pressed", String(UI.freeze));
  const H = K.hello, S = K.state;
  const st = R.frame ? R.frame.st : S && S.spectrum ? S.spectrum.state : "off";
  setText($("#popNote"), (H && H.rta && H.rta.enabled === false ? "Box spectrum DISABLED in k1000.yaml. "
    : "Box spectrum: PGM (" + st + "), with the box's own ballistics and PEAK" + (S && S.spectrum && S.spectrum.on === false ? ": OFF in SETUP › LIVE EQ. " : ". ")) +
    micWords() + "Advisory: nothing applies EQ by itself.");
}
export function renderOverlay() {
  const S = K.state, H = K.hello, fr = R.frame;
  const src = SRC_WORD[UI.prefs.rta] ? UI.prefs.rta : "box";
  let txt = SRC_WORD[src];
  if (src === "box" || src === "both") {
    if (H && H.rta && H.rta.enabled === false) txt = src === "both" ? "MIC" : "RTA OFF";
    else if (UI.freeze) txt += " ❄";
    else if (fr && fr.st !== "live") txt = src === "both" ? "MIC" : fr.st === "waiting" ? "PGM …" : fr.st === "unavailable" ? "NO RTA" : "RTA OFF";
    else if (!fr && S && S.spectrum && S.spectrum.on === false) txt = src === "both" ? "MIC" : "RTA OFF";
  } else if (src === "mic" && UI.freeze) txt += " ❄";
  if (micChosen() && UI.prefs.mic !== "off" && !micAllowed()) txt = txt.replace("MIC", "MIC ?");
  setText($("#srcBtn"), txt + " ▾");
  const ap = activePeak(), tag = $("#peakTag");
  tag.hidden = !ap;
  if (ap) setText(tag.querySelector("span"), (ap.src === "mic" ? "▼ MIC " : "▼ PEAK ") + fmtHz(ap.hz));
  tag.classList.toggle("mic", !!ap && ap.src === "mic");
  if (UI.popOpen) renderPop();
}

export function initGraph() {
  graphEl = $("#graph"); cv = $("#cv"); cx = cv.getContext("2d");
  const cs = getComputedStyle(document.documentElement);
  const col = (n, d) => cs.getPropertyValue("--" + n).trim() || d;
  C = { fg: col("fg", "#fff"), dim: col("dim", "#969696"), ink: col("ink", "#000"), graph: col("graph", "#070707"),
    grid: col("grid", "#262626"), gridHi: col("grid-hi", "#5a5a5a"), axis: col("axis", "#8c8c8c"), amber: col("amber", "#ffb800"),
    green: col("green", "#28dc46"), cut: col("cut", "#ff5a00"), cyan: col("cyan", "#00c8ff"), teal: col("teal", "#0f6476"),
    tealHi: col("teal-hi", "#29a9c4") };
  cv.addEventListener("pointerdown", graphDown);
  cv.addEventListener("contextmenu", (e) => e.preventDefault());
  window.addEventListener("pointermove", (e) => { if (G && e.pointerId === G.pid) graphMove(e); }, { passive: true });
  window.addEventListener("pointerup", (e) => { if (G && e.pointerId === G.pid) graphUp(false); });
  window.addEventListener("pointercancel", (e) => { if (G && e.pointerId === G.pid) graphUp(true); });
  $("#srcBtn").addEventListener("click", () => (UI.popOpen ? closePop() : openPop()));
  $("#popX").addEventListener("click", closePop);
  // a source: BOX · PHONE MIC · BOTH · OFF. PHONE MIC never asks for the mic here (DESIGN §9.15):
  // without a grant it says where to allow it
  $("#popShow").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    UI.prefs.rta = SRC_WORD[b.dataset.v] ? b.dataset.v : "box";
    savePrefs();
    if (micChosen()) {
      micClearErr();
      if (!micAllowed()) { toast(MIC.perm === "denied" ? "MIC BLOCKED: SETUP › THIS PHONE" : "ALLOW THE MIC: SETUP › THIS PHONE", "red", 2600); hap("reject"); }
      else if (UI.prefs.mic === "off") toast("PHONE MIC IS OFF: SETUP › THIS PHONE", "red", 2600);
    }
    hap("tick");
    syncSub(); micSync(); dirty = true; render();
  });
  // FAST / SLOW and the smoothing: the phone mic's line only (the box's bars keep the box's own)
  $("#popMic").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.k === "mspeed") UI.prefs.mspeed = b.dataset.v === "fast" ? "fast" : "slow";
    else UI.prefs.msmooth = [12, 6, 3].includes(+b.dataset.v) ? +b.dataset.v : 6;
    savePrefs(); hap("tick"); dirty = true; render();
  });
  $("#popFreeze").addEventListener("click", () => { UI.freeze = !UI.freeze; syncSub(); dirty = true; render(); });
  $("#measBtn").addEventListener("click", () => { closePop(); toggleMeasure(); dirty = true; });
  // the tag's PEAK is fixed at touch-down too: in BOTH, a mic line that stops mid-hold turns the
  // tag into the box's PEAK under the finger; the hold still grabs the mic PEAK it began on
  bindHold($("#peakTag"), {
    ms: 550, delay: 100,
    begin: () => { const ap = activePeak(); return linked() && ap ? { ap: { hz: ap.hz, src: ap.src } } : null; },
    fire: (ctx) => { hap("arm"); grabPeak(ctx.ap); },
    early: () => toast("HOLD ▼ PEAK = notch helper there"),
  });
  if (window.ResizeObserver) new ResizeObserver(resize).observe(graphEl);
  window.addEventListener("resize", resize);
  resize();
  requestAnimationFrame(loop);
}
export function stopGraphGesture() { if (G) graphUp(true, true); }
onSecondFinger(stopGraphGesture);
