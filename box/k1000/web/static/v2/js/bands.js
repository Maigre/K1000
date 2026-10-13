// SPDX-License-Identifier: GPL-3.0-or-later
/* The band row: LS 1 2 3 4 HS in hello.order (B2's order). A chip TAP selects, nothing else (the
   selection is this phone's, never synced with B2). A chip HOLD of 0.6 s switches that band IN /
   OUT (D-014): `band {s, on}` with the IN / OUT the chip showed at touch-down, sent only when the
   hold completes with the link up, never queued or replayed. 12 px of travel or leaving the chip
   cancels silently and is no tap either. With no link a tap still selects; the hold never fills
   and says so. */

import { K, UI, band, linked, order, render, shortName } from "./store.js";
import { bindHold } from "./hold.js";
import { request } from "./link.js";
import { hap } from "./haptics.js";
import { editOk, toast } from "./view.js";
import { $, $$, h, save, setText } from "./util.js";

const BAND_MS = 600;
let built = "";

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
      begin: () => { const b = band(s); return { nofill: !linked(), want: !(b ? b.on : true) }; },
      fire: (ctx) => (ctx.nofill ? (toast("NO LINK · nothing is sent", "red"), hap("fail")) : toggleBand(s, ctx.want)),
      early: () => { selectBand(s); hap("tick"); },
      lost: () => { toast("LINK LOST · nothing sent · " + shortName(s) + " unchanged", "red", 3000); hap("fail"); },
    });
    c.addEventListener("click", (e) => { if (e.detail === 0) selectBand(s); });   // keyboard only
    el.append(c);
  }
}

export function renderChips() {
  buildChips();
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
      (s === UI.sel ? ", selected" : "") + ". Hold to switch in or out");
  }
}
