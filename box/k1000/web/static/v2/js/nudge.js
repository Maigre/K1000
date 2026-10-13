// SPDX-License-Identifier: GPL-3.0-or-later
/* Steps on a number: `nudge {f, d, fine}` and nothing else (docs/14 §4.1). Never an absolute
   value. A slide's detents wait in a batch per field and leave at most once per slide.send_ms;
   a stepper key sends its step at once. Master and the trims rise through the page's mirror of
   the box's "pa" bucket (and master through the Controller's guard): the excess is dropped and
   said, never queued. The ALIGN lock is the box's, per connection: unlock / lock here, and the
   window shown is the one the box granted (its acks renew it, `locked` ends it). On a link loss
   every waiting detent is thrown away: nothing is replayed (DESIGN §9.3). */

import { Bucket, Guard, NudgeQueue, crossed, stepValue } from "./fields.js";
import { K, UI, canEdit, caps, dropLocal, fieldOf, linked, local, render, setLocal, shown, slide } from "./store.js";
import { on, request } from "./link.js";
import { hap } from "./haptics.js";
import { editOk, toast } from "./view.js";
import { num, now } from "./util.js";

export const queue = new NudgeQueue();
const buckets = new Map();
let guard = null;
const hooks = { refused: () => {}, relocked: () => {} };
export function onRefused(fn) { hooks.refused = fn; }      // (path) -> flash the tiles showing it
export function onRelocked(fn) { hooks.relocked = fn; }    // the ALIGN window closed

on("hello", (H) => {
  buckets.clear();
  const L = H.limits || {};
  guard = new Guard(num(L.guard_db) ? L.guard_db : 6, num(L.guard_window_s) ? L.guard_window_s : 2, now);
});
function bucketFor(fd) {
  const u = fd && fd.up_limit;
  if (!u) return null;
  const name = u.bucket || "pa";
  let b = buckets.get(name);
  if (!b) { b = new Bucket(u.db_per_s, u.burst_db, now); buckets.set(name, b); }
  return b;
}
const isMaster = (path) => path === "master_db";

let slowT = 0;
function slowDown(fd) {
  if (now() - slowT > 900) { toast("SLOW DOWN · " + (isMasterField(fd) ? "master" : "trim") + " up is limited", "red"); slowT = now(); }
  hap("reject");
}
const isMasterField = (fd) => !!fd && fd.label === "MASTER";

// The PA-up mirror: may this rise go out? (the box's bucket and guard stay authoritative)
function paAllows(path, fd, d, fine, rise) {
  if (d <= 0 || !fd || !fd.up_limit) return true;
  const db = (fine && fd.fine ? fd.fine : fd.step) * d;
  if (isMaster(path) && guard && !guard.room(rise)) return false;
  const b = bucketFor(fd);
  if (b && !b.take(db)) return false;
  if (isMaster(path) && guard) guard.spend(rise);
  return true;
}

// One detent (or one accelerated detent, `mult`) of a slide. ctx: the slide ({sticky, start}).
// Returns what the finger should feel: tick | click | reject | swallow | slow | off (no link, or
// a stalled one: the slide stops).
export function slideDetent(path, s, mult, fine, ctx) {
  if (!canEdit()) return "off";
  const fd = fieldOf(path);
  if (!fd) return "off";
  if (fd.lock && !alignOpen()) return "locked";   // the box relocked mid-slide: stop, say it
  if (ctx.sticky) {
    const was = ctx.sticky;
    ctx.sticky = 0;
    if (was === s) return "swallow";               // the extra detent of a sticky stop
  }
  const fineFlag = !!fine && fd.fine !== fd.step;
  const cur = shown(path);
  if (!num(cur)) return "off";
  const d = s * mult;
  const nv = stepValue(fd, cur, d, fineFlag, path);
  if (nv === cur) return "reject";                 // the end of the range: nothing to send
  if (!paAllows(path, fd, d, fineFlag, nv - cur)) { slowDown(fd); return "slow"; }
  let ev = "tick", sticky = false;
  for (const x of fd.sticky || []) if (cur !== x && (nv === x || crossed(cur, nv, x))) { sticky = nv === x; if (sticky) ev = "click"; }
  for (const x of fd.landmarks || []) if (crossed(cur, nv, x)) ev = "click";
  if (ev === "tick" && ctx.start != null && crossed(cur, nv, ctx.start)) ev = "click";
  queue.add(path, d, fineFlag);
  if (sticky) { ctx.sticky = s; queue.seal(path); }
  const p = setLocal(path, nv);
  p.hold = true;
  return ev;
}
// The finger lifted (or the slide was stopped): what waits goes now, then nothing.
export function endSlide(path, flush) {
  if (flush) while (queue.has(path)) sendHead(path);
  else queue.clear(path);
  const p = local.get(path);
  if (p) { p.hold = false; p.until = Math.max(p.until, now() + slide().echo_ms); }
}

// A stepper key: `d` normal detents, one message, now. Master / trim rises may be cut short.
export function stepCmd(path, d) {
  if (!editOk()) return false;
  const fd = fieldOf(path);
  if (!fd) return false;
  if (fd.lock && !alignOpen()) { lockedHint(path); return false; }
  const cur = shown(path);
  if (!num(cur)) return false;
  let n = d;
  if (d > 0 && fd.up_limit) {
    n = 0;
    for (let k = 1; k <= d; k++) {
      const nv = stepValue(fd, cur, n + 1, false, path);
      if (!paAllows(path, fd, 1, false, nv - stepValue(fd, cur, n, false, path))) break;
      n++;
    }
    if (n < d) slowDown(fd);
    if (!n) return false;
  }
  const nv = stepValue(fd, cur, n, false, path);
  if (nv === cur) { hap("reject"); hooks.refused(path); return false; }
  setLocal(path, nv);
  sendNudge(path, n, false);
  hap("tick");
  return true;
}

function sendNudge(path, d, fine) {
  const msg = { t: "nudge", f: path, d, fine: !!fine };
  let id = null;
  id = request(msg, (ok, r) => reply(path, id, ok, r), 2500);
  const p = local.get(path);
  if (p && id != null) p.last = id;
  return id;
}
function sendHead(path) {
  const c = caps(path);
  const m = queue.take(path, c);
  if (!m) return;
  sendNudge(path, m.d, m.fine);
}
// one message per field per slide.send_ms (50 ms: at most 20 a second, docs/14 §3.1.2)
function tick() {
  if (queue.size) {
    if (!canEdit()) queue.clear();               // nothing waits for a stalled link either
    else for (const path of queue.paths()) sendHead(path);
  }
  setTimeout(tick, Math.max(20, Number(slide().send_ms) || 50));
}
setTimeout(tick, 50);

function reply(path, id, ok, r) {
  const p = local.get(path);
  if (ok) {
    if (r && num(r.unlock_s)) { UI.unlockUntil = now() + r.unlock_s * 1000; render(); }
    if (p && p.last === id && !queue.has(path) && r && num(r.v)) {
      p.v = r.v;
      p.until = Math.max(p.until, now() + slide().echo_ms);
    }
    render();
    return;
  }
  const code = r && r.code;
  if (code === "LINK") return;                    // said once, by the link loss
  queue.clear(path);
  if (code === "LOCKED") { relock(false); lockedHint(path, r.why); }
  else if (code === "SLOW") { toast(String(r.why || "SLOW DOWN"), "red"); hap("reject"); }
  else if (code === "TIMEOUT") { /* the next state says the truth */ }
  else if (r && r.why) { toast(String(r.why), "red"); hap("reject"); }
  if (r && num(r.v)) setLocal(path, r.v, slide().echo_ms);
  else if (p) p.until = 0;
  hooks.refused(path);
  render();
}

// ------------------------------------------------------------------ ALIGN (per connection, box-enforced)
export const alignOpen = () => linked() && now() < UI.unlockUntil;
export function lockedHint(path, why) {
  if (path === "lowcut_hz" && K.state && K.state.lowcut_on === false) toast("LO-CUT IS OFF · SETUP › TOPS");
  else toast(why || "LOCKED · HOLD 0.8 s TO EDIT");
  hap("reject");
}
export function requestUnlock() {
  if (UI.unlockPending || !editOk()) return;
  UI.unlockPending = true;
  hap("arm");
  const id = request({ t: "unlock", what: "align" }, (ok, r) => {
    UI.unlockPending = false;
    if (ok && r.v && num(r.v.align_s) && r.v.align_s > 0) {
      UI.unlockUntil = now() + r.v.align_s * 1000;
      toast("DELAY + LO-CUT UNLOCKED · " + Math.round(r.v.align_s) + " s", "white");
      hap("ok");
    } else if (!ok && r.code !== "LINK") { toast(String(r.why || "UNLOCK REFUSED"), "red"); hap("reject"); }
    render();
  }, 2500);
  if (id == null) UI.unlockPending = false;
  render();
}
// The ALIGN window closed: by LOCK (byUser), the box's `locked` or `err LOCKED`, or the page's
// own fallback a second after the window it was granted ran out (main.js). Said once: `was` is
// "the page still held a window", even one that ran out a moment ago (the box's idle `locked`
// arrives a tick after the page's own expiry).
export function relock(byUser) {
  const was = UI.unlockUntil > 0;
  UI.unlockUntil = 0;
  UI.unlockPending = false;
  queue.clear("align_delay_ms"); queue.clear("lowcut_hz");
  if (byUser) {
    request({ t: "lock" }, () => {}, 2500);
    toast("DELAY + LO-CUT LOCKED"); hap("click");
  } else if (was) toast("DELAY + LO-CUT LOCKED AGAIN");
  hooks.relocked();
  render();
}
on("locked", () => relock(false));

// A new connection is a new session: locked, nothing waiting, no local values (docs/14 §2)
on("lost", () => {
  queue.clear();
  UI.unlockUntil = 0; UI.unlockPending = false;
  dropLocal();
});
on("open", () => { UI.unlockUntil = 0; });
