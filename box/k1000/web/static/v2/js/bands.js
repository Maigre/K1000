// SPDX-License-Identifier: GPL-3.0-or-later
/* The band row: LS 1 2 3 4 HS in hello.order (B2's order). A chip TAP selects, nothing else (the
   selection is this phone's, never synced with B2). A chip HOLD of 0.6 s switches that band IN /
   OUT (D-014): `band {s, on}` with the IN / OUT the chip showed at touch-down, sent only when the
   hold completes with the link up, never queued or replayed. 12 px of travel or leaving the chip
   cancels silently and is no tap either. With no link a tap still selects; the hold never fills
   and says so.
   THREE TAPS on the chip that is already selected (each within TAP3_GAP_MS of the last, all within
   TAP3_MS; Thomas, 14/10) flatten that band like GAIN's FLAT key: `band {s, g: 0, on: true}`. A
   tap on another chip only selects it, and starts no count. UNDO shows for UNDO_MS and puts the
   band back only when that is not a rise: a cut, or a band that was OUT. A boost that was IN is
   not given back by a tap (the GAIN steps raise it). UNDO goes when the band moves meanwhile. */

import { K, UI, band, linked, order, render, shortName } from "./store.js";
import { bindHold } from "./hold.js";
import { request } from "./link.js";
import { hap } from "./haptics.js";
import { editOk, toast } from "./view.js";
import { $, $$, h, now, num, save, setText } from "./util.js";

const BAND_MS = 600;
const TAP3_GAP_MS = 450, TAP3_MS = 1000, UNDO_MS = 5000;
let built = "";
let taps = null;                            // {s, n, t0, last}: taps on the selected chip
let undo = null;                            // {s, g, on, timer}: what FLAT replaced

export function selectBand(s) {
  if (s == null || s < 0) return;
  UI.sel = s;
  save("k1000v2.sel", s);
  render();
}

function toggleBand(s, wantOn) {
  if (!linked()) { toast("NO LINK · nothing is sent · " + shortName(s) + " unchanged", "red", 2600); hap("fail"); return; }
  if (!editOk()) return;
  UI.sel = s;
  save("k1000v2.sel", s);
  hap("click");
  const id = request({ t: "band", s, on: wantOn }, (ok, r) => {     // its own id: a late ack never confirms another press
    if (ok) {
      const on = r.v && typeof r.v.on === "boolean" ? r.v.on : wantOn;
      toast(shortName(s) + (on ? " IN" : " OUT"), on ? "" : "white", 1600);
    } else if (r.code === "LINK") {
      toast("LINK LOST · " + shortName(s) + " not confirmed", "red", 2600);
    } else if (r.code !== "TIMEOUT") { toast(String(r.why || "refused"), "red"); hap("reject"); }
    else { toast("No answer from K1000 · " + shortName(s) + " may be unchanged", "red", 2600); hap("fail"); }
    render();
  }, 1500);
  if (id == null) { toast("NO LINK · nothing is sent", "red"); hap("fail"); }
  render();
}

// ------------------------------------------------------------------ three taps = FLAT
function countTap(s, wasSel) {
  const t = now();
  if (!wasSel) { taps = null; return; }           // this tap selected the chip: no count yet
  if (!taps || taps.s !== s || t - taps.last > TAP3_GAP_MS) taps = { s, n: 0, t0: t, last: t };
  taps.n++;
  taps.last = t;
  if (taps.n < 3) return;
  const quick = t - taps.t0 <= TAP3_MS;
  taps = null;
  if (quick) flatTap(s);
}

function flatTap(s) {
  const b = band(s);
  if (!b) return;
  if (b.on && b.g === 0) { toast(shortName(s) + " ALREADY FLAT", "dim", 1600); hap("tick"); return; }
  if (!linked()) { toast("NO LINK · nothing is sent · " + shortName(s) + " unchanged", "red", 2600); hap("fail"); return; }
  if (!editOk()) return;
  const was = { g: b.g, on: b.on };
  hap("click");
  const id = request({ t: "band", s, g: 0, on: true }, (ok, r) => {
    if (ok) {
      const back = !was.on || !(was.g > 0);          // a cut, or OUT: UNDO is no rise
      toast(shortName(s) + " FLAT" + (was.on ? "" : " · IN") + (back ? "" : " · boost gone"), "", back ? UNDO_MS : 2200);
      if (back) offerUndo(s, was); else dropUndo();
    } else if (r.code === "LINK") {
      toast("LINK LOST · " + shortName(s) + " not confirmed", "red", 2600);
    } else if (r.code !== "TIMEOUT") { toast(String(r.why || "refused"), "red"); hap("reject"); }
    else { toast("No answer from K1000 · " + shortName(s) + " may be unchanged", "red", 2600); hap("fail"); }
    render();
  }, 2500);
  if (id == null) { toast("NO LINK · nothing is sent", "red"); hap("fail"); }
  render();
}

function offerUndo(s, was) {
  dropUndo();
  const el = $("#undo");
  if (!el) return;
  setText(el, "UNDO " + shortName(s) + " FLAT");
  el.hidden = false;
  undo = { s, g: was.g, on: was.on, t: now(), seen: false, timer: setTimeout(dropUndo, UNDO_MS) };
}

function dropUndo() {
  if (undo) clearTimeout(undo.timer);
  undo = null;
  const el = $("#undo");
  if (el) el.hidden = true;
}

function doUndo() {
  const u = undo;
  if (!u) return;
  dropUndo();
  const b = band(u.s);
  if (!b || !b.on || b.g !== 0) { toast(shortName(u.s) + " changed meanwhile · nothing sent", "red", 2600); hap("reject"); return; }
  if (!linked()) { toast("NO LINK · nothing is sent · " + shortName(u.s) + " stays flat", "red", 2600); hap("fail"); return; }
  if (!editOk()) return;
  hap("click");
  const id = request({ t: "band", s: u.s, g: u.g, on: u.on }, (ok, r) => {
    if (ok) toast(shortName(u.s) + " BACK" + (u.on ? "" : " · OUT"), "white", 1600);
    else if (r.code === "LINK") toast("LINK LOST · " + shortName(u.s) + " not confirmed", "red", 2600);
    else if (r.code !== "TIMEOUT") { toast(String(r.why || "refused"), "red"); hap("reject"); }
    else { toast("No answer from K1000 · " + shortName(u.s) + " may be unchanged", "red", 2600); hap("fail"); }
    render();
  }, 2500);
  if (id == null) { toast("NO LINK · nothing is sent", "red"); hap("fail"); }
}

// UNDO stands only while the band is still what FLAT made it, the link is up and LIVE is shown.
// The ack comes before the state frame that shows the band flat: until that frame (1.5 s at
// most) the old values are no "change meanwhile"; doUndo checks the band again anyway.
export function renderUndo() {
  if (!undo) return;
  const b = band(undo.s);
  const flat = !!b && b.on === true && num(b.g) && b.g === 0;
  if (flat) undo.seen = true;
  if (!linked() || UI.sheet || (!flat && (undo.seen || now() - undo.t > 1500))) dropUndo();
}

let undoBound = false;
function bindUndo() {
  const el = $("#undo");
  if (!el || undoBound) return;
  undoBound = true;
  el.addEventListener("click", doUndo);
}

export function buildChips() {
  const el = $("#bands");
  const key = order().join(",") + "|" + order().map(shortName).join(",");
  if (built === key) return;
  built = key;
  el.textContent = "";
  for (const s of order()) {
    const c = h("button", "chip", '<b class="fill"></b><span></span><em class="otag" hidden>OUT</em><em class="etag" hidden>E</em>');
    setText(c.querySelector("span"), shortName(s));
    c.dataset.s = String(s);
    bindHold(c, {
      ms: BAND_MS, delay: 150, tapPx: K.hello && K.hello.slide ? K.hello.slide.tap_px : 10,
      // the intent is fixed at touch-down: the IN / OUT the chip showed then
      begin: () => { const b = band(s); return { nofill: !linked(), want: !(b ? b.on : true), wasSel: UI.sel === s }; },
      fire: (ctx) => (ctx.nofill ? (toast("NO LINK · nothing is sent", "red"), hap("fail")) : toggleBand(s, ctx.want)),
      early: (ctx) => { selectBand(s); hap("tick"); countTap(s, ctx.wasSel); },
      lost: () => { toast("LINK LOST · nothing sent · " + shortName(s) + " unchanged", "red", 3000); hap("fail"); },
    });
    c.addEventListener("click", (e) => { if (e.detail === 0) selectBand(s); });   // keyboard only
    el.append(c);
  }
}

export function renderChips() {
  buildChips();
  bindUndo();
  renderUndo();
  const S = K.state;
  for (const c of $$(".chip", $("#bands"))) {
    const s = Number(c.dataset.s), b = band(s);
    const g = b ? b.g : 0, on = b ? b.on : true;
    c.classList.toggle("cut", on && g < 0);
    c.classList.toggle("boost", on && g > 0);
    c.classList.toggle("out", !on);
    c.classList.toggle("cutc", !on && g < 0);
    c.classList.toggle("boostc", !on && g > 0);
    c.classList.toggle("sel", s === UI.sel);
    c.querySelector(".otag").hidden = on;
    c.querySelector(".etag").hidden = !(S && S.box_band === s);
    c.setAttribute("aria-label", shortName(s) + (on ? "" : " out") + (S && S.box_band === s ? " (knobs)" : "") +
      (s === UI.sel ? ", selected. Tap three times: flat" : "") + ". Hold to switch in or out");
  }
}
