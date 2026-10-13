// SPDX-License-Identifier: GPL-3.0-or-later
/* Small helpers shared by every module of the v2 page. No DOM work at import time, so the pure
   parts (formatting) also run under node (box/tools/v2lab/unit.mjs). */

export const MINUS = "−";
export const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const num = (v) => typeof v === "number" && Number.isFinite(v);

export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

// An element from TRUSTED markup (our own literals only). Anything the box sends goes in with
// setText / textContent, never as HTML.
export function h(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  if (tag === "button") e.type = "button";
  return e;
}
export function setText(el, t) {
  t = t == null ? "" : String(t);
  if (el && el.textContent !== t) el.textContent = t;
}

export function load(k, d) {
  try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; }
}
export function save(k, v) {
  try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode: fine */ }
}
export function sget(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
export function sset(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* fine */ } }

// ------------------------------------------------------------------ formatting (box words, box units)
export function fmtDb(v) {
  if (!num(v)) return "--";
  v = Math.round(v * 10) / 10;
  if (Math.abs(v) < 0.05) return "0.0";
  return (v > 0 ? "+" : MINUS) + Math.abs(v).toFixed(1);
}
export function fmtHz(f) {
  if (!num(f)) return "--";
  if (f >= 10000) return (f / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  if (f >= 1000) return (f / 1000).toFixed(2) + "k";
  if (f >= 100) return String(Math.round(f));
  return f.toFixed(1);
}
export function fmtQ(q) { return !num(q) ? "--" : q >= 10 ? q.toFixed(1) : q.toFixed(2); }
export function bwOct(q) { return (2 / Math.LN2) * Math.asinh(1 / (2 * q)); }
const FRAC = [[1 / 12, "1/12"], [1 / 8, "⅛"], [1 / 6, "⅙"], [1 / 5, "⅕"], [1 / 4, "¼"], [1 / 3, "⅓"],
  [1 / 2, "½"], [2 / 3, "⅔"], [1, "1"], [1.5, "1½"], [2, "2"], [3, "3"]];
export function fmtOct(o) {
  if (!num(o)) return "--";
  for (const [x, g] of FRAC) if (Math.abs(o / x - 1) < 0.04) return g + " oct";
  return (o < 1 ? o.toFixed(2) : o.toFixed(1)) + " oct";
}
export function fmtMs(v) {
  if (!num(v)) return "--";
  if (Math.abs(v) < 1e-6) return "0.00";
  return (v > 0 ? "+" : MINUS) + Math.abs(v).toFixed(2);
}
// DELAY's words (D-011): > 0 subs later, < 0 tops later. short = a tile's sub-line
export function delaySub(v, short) {
  if (!num(v) || Math.abs(v) < 1e-6) return short ? "aligned" : "ALIGNED";
  if (short) return v > 0 ? "subs later" : "tops later";
  return (v > 0 ? "SUBS LATER" : "TOPS LATER") + " · " + (Math.abs(v) * 0.343).toFixed(2) + " m";
}
export function mmss(s) {
  s = Math.max(0, Math.ceil(s));
  const hh = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return (hh ? hh + ":" + String(m).padStart(2, "0") : String(m)) + ":" + String(ss).padStart(2, "0");
}
// a signed step for a key label: 0.5 -> "+½", -3 -> "−3", 0.1 -> "+0.1"
export function fmtSigned(x) {
  const s = x > 0 ? "+" : MINUS, a = Math.abs(x);
  const whole = Math.floor(a + 1e-9), frac = a - whole;
  if (Math.abs(frac - 0.5) < 1e-9) return s + (whole ? whole : "") + "½";
  if (frac < 1e-9) return s + whole;
  return s + String(Math.round(a * 1000) / 1000);
}
