// SPDX-License-Identifier: GPL-3.0-or-later
/* Hold-to-act keys (MUTE, band chips, FLAT, ▼ PEAK, SET of a rebuild row, the 1 s actions).
   One rule for all of them (DESIGN §5.2, §5.6, §9.1): a hold that travels more than HOLD_SLOP
   px from where it touched down, in any direction, or leaves the key, cancels silently, so a
   swipe never acts. One hold at a time, except that MUTE (o.preempt) takes over: the hold in
   progress is cancelled silently and MUTE's starts (feedback: a thumb on a chip or ▼ PEAK must
   never keep the other thumb from muting). A second finger cancels any other hold, and never
   starts one (DESIGN §5.3). A key that leaves the page or is hidden mid-hold (the stepper's
   idle close, a sheet or tab closed) cancels its hold: nothing fires after the finger lifted.
   A link loss cancels the hold and says so; it is never replayed (§9.3).
   o.begin(e) -> ctx ({ms?, nofill?, valid?, why?}) or null (no hold); o.fire(ctx) when it
   completes; o.early(ctx) on a still release before then (only under o.tapPx of travel, when
   given); o.lost(ctx) when the link took it. ctx.valid(), when given, is checked every frame
   and at the end: false cancels the hold and says ctx.why. */

import { clamp, now } from "./util.js";
import { toast } from "./view.js";
import { hap } from "./haptics.js";
import { onSecondFinger, ptrs } from "./touch.js";

export const HOLD_SLOP = 12;
let active = null;                          // {abort, lost, ctx, preempt}
export const holdActive = () => !!active;

// a second finger: every hold but MUTE's is cancelled, silently (the second finger may be the
// one going for MUTE; MUTE's own hold is never stopped by another finger)
onSecondFinger(() => {
  if (!active || active.preempt) return;
  const a = active;
  active = null;
  a.abort();
});
// the key is gone from the page, or hidden: its hold cannot complete
const gone = (el) => !el.isConnected || el.getClientRects().length === 0;

// The link dropped: the hold in progress is cancelled, and said (by its own o.lost, or the caller)
export function dropHolds() {
  const a = active;
  active = null;
  if (a && a.abort()) { if (a.lost) { a.lost(a.ctx); return true; } }
  return false;
}

export function setFill(el, p) {
  const f = el && el.querySelector(":scope > .fill");
  if (f) f.style.width = (clamp(p, 0, 1) * 100).toFixed(1) + "%";
}

export function bindHold(el, o) {
  let st = null;
  const abort = () => {
    if (!st || st.done) return false;
    clearTimeout(st.timer); setFill(el, 0); st = null;
    return true;
  };
  const broken = () => st && st.ctx.valid && !st.ctx.valid();
  const quit = () => {                                // silent
    abort();
    if (active && active.abort === abort) active = null;
  };
  const cancelSaying = () => {
    const why = st && st.ctx.why;
    abort();
    if (active && active.abort === abort) active = null;
    if (why) { toast(why, "red", 2600); hap("reject"); }
  };
  el.addEventListener("pointerdown", (e) => {
    if (st || e.button > 0) return;
    if (!o.preempt && ptrs.size > 1) return;         // a second finger never starts a hold
    if (active && active.abort !== abort) {
      if (!o.preempt) return;
      const other = active;                          // MUTE takes over, the other hold ends silently
      active = null;
      other.abort();
    }
    const ctx = o.begin ? o.begin(e) : {};
    if (!ctx) return;
    st = { id: e.pointerId, x: e.clientX, y: e.clientY, t0: now(), ms: ctx.ms || o.ms, ctx, done: false, maxD: 0 };
    active = { abort, lost: o.lost, ctx, preempt: !!o.preempt };
    try { el.setPointerCapture(e.pointerId); } catch (err) { /* fine */ }
    const s0 = st;
    st.timer = setTimeout(() => {
      if (st !== s0) return;
      if (gone(el)) { quit(); return; }
      if (broken()) { cancelSaying(); return; }
      st.done = true;
      if (active && active.abort === abort) active = null;
      setFill(el, 0);
      o.fire(st.ctx);
    }, st.ms);
    const anim = () => {
      if (st !== s0 || st.done) return;
      if (gone(el)) { quit(); return; }
      if (broken()) { cancelSaying(); return; }
      const dl = o.delay || 0;
      if (!st.ctx.nofill) setFill(el, (now() - st.t0 - dl) / (st.ms - dl));
      requestAnimationFrame(anim);
    };
    requestAnimationFrame(anim);
  });
  const end = (e, cancel) => {
    if (!st || e.pointerId !== st.id) return;
    const s = st;
    st = null;
    if (active && active.abort === abort) active = null;
    clearTimeout(s.timer);
    setFill(el, 0);
    if (e.clientX != null) s.maxD = Math.max(s.maxD, Math.hypot(e.clientX - s.x, e.clientY - s.y));
    if (!s.done && !cancel && o.early && (o.tapPx == null || s.maxD < o.tapPx)) o.early(s.ctx);
  };
  el.addEventListener("pointerup", (e) => end(e, false));
  el.addEventListener("pointercancel", (e) => end(e, true));
  el.addEventListener("pointermove", (e) => {
    if (!st || e.pointerId !== st.id || st.done) return;
    const d = Math.hypot(e.clientX - st.x, e.clientY - st.y);
    st.maxD = Math.max(st.maxD, d);
    const r = el.getBoundingClientRect();
    const off = e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    if (off || d > HOLD_SLOP) quit();                // cancels silently, and is no tap either
  });
  // the capture went (the key left the page, the browser took the pointer): cancelled, silently
  el.addEventListener("lostpointercapture", (e) => { if (st && e.pointerId === st.id && !st.done) quit(); });
  el.addEventListener("contextmenu", (e) => e.preventDefault());
}

// An early release: the key says "hold" for a moment
export function earlyFlash(el, txt, ms, after) {
  const sp = el.querySelector(":scope > span");
  if (!sp) return;
  if (el._early == null) el._early = sp.textContent;
  el.classList.add("early");
  sp.textContent = txt || "hold";
  clearTimeout(el._earlyT);
  el._earlyT = setTimeout(() => {
    el.classList.remove("early");
    sp.textContent = el._early; el._early = null;
    if (after) after();
  }, ms || 700);
}
