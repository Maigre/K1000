// SPDX-License-Identifier: GPL-3.0-or-later
/* The value-tile rules that do not touch the DOM (docs/14 §3.1.1-§3.1.2, DESIGN §5.3-§5.4):
   the box's step arithmetic (to show a value before the box acks it), the slide's speed ->
   step mapping, the stepper key labels, the PA-up bucket mirror and the nudge batches.
   Every number comes from hello.fields / hello.slide: nothing the box enforces is hard-coded.
   Pure: box/tools/v2lab/unit.mjs runs it under node. */

import { MINUS, clamp, fmtSigned, num } from "./util.js";

// "live_peq.2.gain" -> "live_peq.gain", "out_trim_db.1" -> "out_trim_db" (docs/14 §3.1.1)
export function generic(path) { return String(path).replace(/\.\d+(?=\.|$)/, ""); }

function roundTo(v, digits) {
  const p = Math.pow(10, Number.isInteger(digits) ? digits : 6);
  return Math.round(v * p) / p + 0;
}

// The box's Range.nudge (state.py), plus ALIGN's rule (nudge_align_delay: a move that would cross
// 0 stops at 0). `fd` = hello.fields[generic(path)].
export function stepValue(fd, v, d, fine, path) {
  if (!fd || !num(v) || !d) return v;
  const step = fine && fd.fine ? fd.fine : fd.step;
  let nv;
  if (fd.log) nv = v * Math.pow(2, step * d);
  else if (fd.snap) {
    const q = v / step;
    const base = d > 0 ? Math.floor(q + 1e-9) : Math.ceil(q - 1e-9);
    nv = (base + d) * step;
  } else nv = v + step * d;
  nv = roundTo(clamp(nv, fd.min, fd.max), fd.digits);
  if (path === "align_delay_ms" && ((v > 0 && nv < 0) || (v < 0 && nv > 0))) nv = 0;
  return nv;
}

// Speed (px/s at this phone's sensitivity) -> 0 (fine) or the multiplier (docs/14 §3.1.2): the
// knob driver's accel_multiplier. cap = fields[k].accel.up or .down.
export function speedMult(vPxS, slide, cap) {
  cap = Math.max(1, cap | 0);
  if (!(vPxS >= slide.fine_below_px_s)) return 0;
  if (vPxS <= slide.accel_from_px_s) return 1;
  if (vPxS >= slide.accel_full_px_s) return cap;
  const m = 1 + Math.floor((cap - 1) * (vPxS - slide.accel_from_px_s) / (slide.accel_full_px_s - slide.accel_from_px_s));
  return clamp(m, 1, cap);
}

// Values the slide stops on (one extra detent of travel) or clicks on passing.
export function crossed(a, b, x) { return (a < x && b >= x) || (a > x && b <= x); }

// ------------------------------------------------------------------ stepper key labels
// One key = `d` normal detents (fields[k].stepper). The label says what it does in the unit.
// Q / SLOPE (a log field): a key that halves or doubles Q reads ×½ / ×2 (DESIGN §5.4: the
// box's stepper ±6 of ⅙ oct); one detent reads − / +; any other count is said as it is (±2).
export function stepLabel(fd, d, opts = {}) {
  if (!fd) return [String(d), ""];
  if (opts.q) {
    const k = Math.abs(d), f = fd.log && num(fd.step) ? Math.pow(2, fd.step * k) : 0;
    const big = Math.abs(f - 2) < 0.02 ? (d < 0 ? "×½" : "×2") : k === 1 ? (d < 0 ? MINUS : "+") : fmtSigned(d);
    return [big, opts.shelf ? (d < 0 ? "softer" : "steeper") : d < 0 ? "wider" : "narrower"];
  }
  const x = fd.step * d;
  if (fd.log) {
    const o = Math.abs(x);
    const g = Math.abs(o - 1 / 3) < 1e-3 ? "⅓" : Math.abs(o - 1 / 12) < 1e-3 ? "1/12" : Math.abs(o - 1 / 6) < 1e-3 ? "⅙"
      : Math.abs(o - 1 / 4) < 1e-3 ? "¼" : Math.abs(o - 1 / 2) < 1e-3 ? "½" : String(Math.round(o * 100) / 100);
    return [(x > 0 ? "+" : MINUS) + g, Math.abs(o - 1 / 12) < 1e-3 ? "" : "oct"];
  }
  if (fd.unit === "ms") {
    const big = Math.abs(x) >= 1 - 1e-9;
    return [(x > 0 ? "+" : MINUS) + (big ? String(Math.round(Math.abs(x))) : Math.abs(x).toFixed(1)), big ? "ms" : ""];
  }
  return [fmtSigned(Math.round(x * 1000) / 1000), ""];
}

// ------------------------------------------------------------------ the PA-up mirror (docs/14 §6)
// One token bucket per `up_limit.bucket` name: master and the trims spend the same dB.
export class Bucket {
  constructor(rate, burst, clock) {
    this.rate = rate; this.burst = burst; this.tokens = burst; this.clock = clock; this.t = clock();
  }
  take(n) {
    const t = this.clock();
    this.tokens = Math.min(this.burst, this.tokens + (t - this.t) / 1000 * this.rate);
    this.t = t;
    if (this.tokens + 1e-9 < n) return false;
    this.tokens -= n;
    return true;
  }
}
// The Controller's own master guard (limits.guard_db within limits.guard_window_s), mirrored.
export class Guard {
  constructor(db, windowS, clock) { this.db = db; this.ms = windowS * 1000; this.clock = clock; this.ups = []; }
  room(n) {
    if (!(this.db > 0)) return true;
    const t = this.clock();
    while (this.ups.length && t - this.ups[0][0] >= this.ms) this.ups.shift();
    const used = this.ups.reduce((a, u) => a + u[1], 0);
    return used + n <= this.db + 1e-9;
  }
  spend(n) { this.ups.push([this.clock(), n]); }
}

// ------------------------------------------------------------------ nudge batches
// Detents wait here until the next send tick (one message per field every slide.send_ms). A
// batch has one `fine` flag and one direction; a sticky stop seals it, so the box's value after
// the message is the value the page showed at that stop. take() splits at the field's caps.
export class NudgeQueue {
  constructor() { this.q = new Map(); }
  add(path, d, fine) {
    if (!d) return;
    let arr = this.q.get(path);
    if (!arr) { arr = []; this.q.set(path, arr); }
    const last = arr[arr.length - 1];
    if (last && !last.sealed && last.fine === fine && Math.sign(last.d) === Math.sign(d)) last.d += d;
    else arr.push({ d, fine, sealed: false });
  }
  seal(path) { const arr = this.q.get(path); if (arr && arr.length) arr[arr.length - 1].sealed = true; }
  take(path, caps) {
    const arr = this.q.get(path);
    if (!arr || !arr.length) return null;
    const b = arr[0];
    const lo = caps ? caps[0] : -Infinity, hi = caps ? caps[1] : Infinity;
    const d = clamp(b.d, lo, hi);
    b.d -= d;
    if (!b.d) arr.shift();
    if (!arr.length) this.q.delete(path);
    return d ? { d, fine: b.fine } : null;
  }
  has(path) { return this.q.has(path); }
  paths() { return Array.from(this.q.keys()); }
  clear(path) { if (path == null) this.q.clear(); else this.q.delete(path); }
  get size() { return this.q.size; }
}
