// SPDX-License-Identifier: GPL-3.0-or-later
/* K1000 phone v2 (DESIGN §5-§9, contract docs/14-phone-v2-protocol.md). One LIVE page: graph ·
   band row ⇄ stepper · FREQ GAIN Q · BAL DELAY LO-CUT · MASTER, meters, MUTE; SETUP and STATUS as
   sheets that stop above the footer. The box is the truth: every rule it enforces (mute, master
   and trim rises, the ALIGN lock, choose-then-set, the notch helper) is the box's, and the page
   hard-codes none of its values (hello.fields, hello.slide, hello.setup).

   Live-safety rules kept here (DESIGN §9): MUTE in every view, hold 0.6 / 1.2 s, a swipe never
   mutes, never rate-limited · no single tap changes the sound · nothing happens after the
   finger lifts (no inertia, no auto-repeat, no queue; nothing replayed after a link loss) ·
   master up 0.5 dB per 20 px, never accelerated · DELAY / LO-CUT only inside the box's unlock ·
   on a link loss the controls dim at once and the fallback is named · a box-update reload waits
   for 2 s without a finger · the phone never asks for a permission from LIVE. */

import "./native.js";                // the APK handshake listener, registered at module top level
import { K, UI, linkSlow, linked, onRender, render, renderNow } from "./store.js";
import { connect, on, setBusyCheck, stats, BUILD } from "./link.js";
import { buildMeters, drawMeters, lastToastAge, layout, renderPills, renderStrip, toast, toastLog } from "./view.js";
import { TILES, makeTile, renderTiles, stopGesture, tileGesture } from "./tile.js";
import { closeStepper, renderStepper, stepperOpen } from "./stepper.js";
import { renderChips, selectBand } from "./bands.js";
import { closePop, graphGesture, initGraph, markDirty, renderOverlay, stopGraphGesture } from "./graph.js";
import { initMute, mutePending, renderMute } from "./mute.js";
import { backInSheet, drawInMeter, initSheets, renderSheet, stateArrived } from "./sheets.js";
import { R, syncSub } from "./rta.js";
import { dropHolds, holdActive } from "./hold.js";
import { hapLog } from "./haptics.js";
import { queue, relock } from "./nudge.js";
import { onReady, nativeSend, refused as nativeRefused, sendExclusion, setBackHandler, can } from "./native.js";
import { $, load, now } from "./util.js";

// ------------------------------------------------------------------ build the controls
for (const tk of ["freq", "gain", "q"]) $("#tiles").append(makeTile(tk));
for (const tk of ["bal", "delay", "lowcut"]) $("#alignRow").append(makeTile(tk));
const master = makeTile("master");
master.classList.add("ftile");
$("#footTile").replaceWith(master);
buildMeters();
initGraph();
initMute();
initSheets();

// ------------------------------------------------------------------ render
// MUTE first, and every part on its own: a box message of an odd shape that breaks one part
// never leaves the MUTE key showing the wrong thing (the error still reaches the console)
const safely = (f) => { try { return f(); } catch (e) { setTimeout(() => { throw e; }, 0); return undefined; } };
function renderAll() {
  safely(renderMute);
  const alertMoved = safely(renderStrip);
  safely(renderChips);
  safely(renderTiles);
  safely(renderStepper);
  safely(renderPills);
  safely(renderOverlay);
  safely(renderSheet);
  $("#foot").classList.toggle("left", UI.prefs.side === "left");
  markDirty();
  if (alertMoved) requestAnimationFrame(() => { layout(); exclusion(); });
  else safely(exclusion);                 // a no-op unless the APK has the cap and a rect moved
}
onRender(renderAll);

on("hello", (H) => {
  if (UI.sel == null || UI.sel >= H.bands.length) {
    const saved = load("k1000v2.sel", null);
    UI.sel = Number.isInteger(saved) && saved >= 0 && saved < H.bands.length ? saved : null;
  }
  render();
});
on("state", (S) => {
  if (UI.sel == null) UI.sel = Number.isInteger(S.box_band) ? S.box_band : (K.hello && K.hello.order && K.hello.order.length ? K.hello.order[0] : 0);
  stateArrived();
  boxHint(S);
});
on("m", () => { drawMeters(); drawInMeter(); });
on("rta", () => markDirty());

// The box's own hints (its screen's words) become a toast, unless this phone said something in
// the last 1.5 s (its own words for its own action win: the box's echo of it is dropped).
let lastHint = "";
function boxHint(S) {
  const t = String(S.hint || "");
  if (t && t !== lastHint && t !== toastLog[toastLog.length - 1] && lastToastAge() > 1500) {
    toast(t, "dim", Math.min(3000, Math.max(1200, (S.hint_s || 2) * 1000)));
  }
  lastHint = t;
}

// ------------------------------------------------------------------ link loss: dim at once, drop, say
on("lost", () => {
  stopGesture();
  stopGraphGesture();
  const said = dropHolds();
  closeStepper(true);
  if (!said) toast("LINK LOST · controls dim · to mute: " + ((K.state && K.state.mute_fallback) || "use B4 on the box"), "red", 2600);
  renderNow();
});
on("open", () => render());
setBusyCheck(() => mutePending() || holdActive() || !!tileGesture() || !!graphGesture() || queue.size > 0);

// ------------------------------------------------------------------ Back (APK) and Esc (browser)
function back() {
  if (stepperOpen()) { closeStepper(); return true; }
  if (closePop()) { render(); return true; }
  if (backInSheet()) return true;
  return false;
}
setBackHandler(back);
window.addEventListener("keydown", (e) => { if (e.key === "Escape") back(); });

// ------------------------------------------------------------------ the APK: exclusion rects, keep awake
function exclusion() {
  if (!can("exclusion")) return;
  const W = window.innerWidth;
  const rows = UI.sheet ? ["#sheet", "#foot"] : ["#slot", "#tiles", "#alignRow", "#foot"];
  const rects = [];
  for (const sel of rows) {
    const el = $(sel);
    if (!el || el.hidden) continue;
    const r = el.getBoundingClientRect();
    if (r.height > 0) rects.push([0, r.top, W, r.height]);
  }
  sendExclusion(rects);
}
onReady(() => {
  nativeSend({ t: "keep_awake", on: true });
  requestAnimationFrame(() => { layout(); exclusion(); });
});

// ------------------------------------------------------------------ housekeeping, 4 Hz
setInterval(() => {
  // the ALIGN window the box granted ran out: the box's `locked` relocks (and says it) a tick
  // later; a second on, the page relocks by itself, said the same way
  if (UI.unlockUntil && now() > UI.unlockUntil + 1000) relock(false);
  syncSub();
  render();
}, 250);
document.addEventListener("visibilitychange", () => syncSub());

const ro = window.ResizeObserver ? new ResizeObserver(() => { layout(); exclusion(); render(); }) : null;
if (ro) ro.observe($("#app"));
window.addEventListener("resize", () => { layout(); exclusion(); });

// keep the screen awake in a browser too (the APK keeps it itself)
let wakeLock = null;
async function wake() {
  if (!("wakeLock" in navigator) || document.hidden || wakeLock) return;
  try { wakeLock = await navigator.wakeLock.request("screen"); wakeLock.addEventListener("release", () => { wakeLock = null; }); }
  catch (e) { wakeLock = null; }
}
document.addEventListener("pointerdown", wake, { passive: true });
on("open", wake);

// the v2 worker, scope /v2/ (the box serves /v2/sw.js with Service-Worker-Allowed: /v2/)
let sw = "none";
if ("serviceWorker" in navigator && window.isSecureContext) {
  sw = "registering";
  navigator.serviceWorker.register("sw.js", { scope: "./" })
    .then((r) => { sw = "registered " + r.scope; })
    .catch((e) => { sw = "failed: " + (e && e.message ? e.message : e); });   // still works as a page
}

// for the lab (box/tools/v2lab) and the STATUS sheet: read-only views
window.K1000v2 = {
  build: BUILD, stats, hapLog, toasts: toastLog, sw: () => sw,
  state: () => K.state, hello: () => K.hello, link: () => K.link, linked, slow: linkSlow, native: () => (UI.native ? Object.assign({}, UI.native) : null),
  nativeRefused: () => nativeRefused.slice(),
  ui: () => ({ sel: UI.sel, field: UI.field, sheet: UI.sheet, tab: UI.tab, pick: UI.pick ? { f: UI.pick.f, v: UI.pick.v } : null,
    unlocked: UI.unlockUntil > now(), unlockLeft: Math.max(0, UI.unlockUntil - now()), prefs: Object.assign({}, UI.prefs), popOpen: UI.popOpen }),
  rta: () => ({ subscribed: R.subscribed, frames: R.frames, st: R.frame ? R.frame.st : null, peak: R.frame ? R.frame.peak_hz : null }),
  tiles: () => TILES.map((t) => ({ tk: t._tk, sheet: t._sheet, v: t.querySelector(".tv span").textContent })),
  select: (s) => selectBand(s),
};

layout();
renderAll();
connect();
