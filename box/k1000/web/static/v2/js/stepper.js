// SPDX-License-Identifier: GPL-3.0-or-later
/* The stepper: a tile's tap pops its steps into a fixed place, the band row's slot (a SETUP
   number: under its row), never a floating bubble (DESIGN §5.4). Keys fire on release, one step
   per tap, no auto-repeat; the keys are hello.fields[k].stepper (normal detents). GAIN adds FLAT
   (hold 0.6 s): `band {s, g: 0, on: true}`, 0 dB and back IN. DELAY and LO-CUT get LOCK in the
   label cell (relocks both). Closes on ✕, the tile, Back, a tap on empty graph, or
   slide.stepper_idle_s idle; the idle clock stops while a finger is on the steps (a FLAT hold
   never straddles the close) and restarts when it lifts. */

import { UI, band, bandKind, fieldOf, linked, pathOf, render, shortName, slide } from "./store.js";
import { relock, stepCmd, lockedHint, alignOpen, onRelocked } from "./nudge.js";
import { stepLabel } from "./fields.js";
import { bindHold, earlyFlash } from "./hold.js";
import { request } from "./link.js";
import { hap } from "./haptics.js";
import { editOk, toast } from "./view.js";
import { TILES, hudShow, renderTile, tileLabel } from "./tile.js";
import { $, h, setText } from "./util.js";

let idleT = 0;
const pressed = new Set();                    // pointers down on the steps
export function armIdle() {
  clearTimeout(idleT);
  if (pressed.size) return;                   // re-armed when the finger lifts
  idleT = setTimeout(() => closeStepper(), (Number(slide().stepper_idle_s) || 8) * 1000);
}
// a finger on a step key (FLAT's 0.6 s hold above all): no idle close under it
function watchPresses(host) {
  if (host._watched) return;
  host._watched = true;
  host.addEventListener("pointerdown", (e) => { pressed.add(e.pointerId); clearTimeout(idleT); }, true);
}
// (window, capture: the key under the finger may be gone by then)
const lifted = (e) => {
  if (!pressed.delete(e.pointerId)) return;
  if (UI.field) armIdle();
};
window.addEventListener("pointerup", lifted, true);
window.addEventListener("pointercancel", lifted, true);
export function toggleStepper(tk, tile) {
  if (UI.field === tk && UI.host === tile) { closeStepper(); return; }
  openStepper(tk, tile);
}
export function openStepper(tk, tile) {
  closeStepper(true);
  UI.field = tk;
  UI.host = tile || TILES.find((t) => t._tk === tk && !t._sheet) || null;
  const stepperEl = $("#stepper");
  const host = UI.host && UI.host._sheet ? UI.host._ist : stepperEl;
  if (!host) { UI.field = null; UI.host = null; return; }
  buildSteps(tk, host, host === stepperEl);
  watchPresses(host);
  if (host === stepperEl) { $("#bands").hidden = true; stepperEl.hidden = false; }
  else host.hidden = false;
  armIdle();
  render();
}
export function closeStepper(quiet) {
  clearTimeout(idleT);
  pressed.clear();
  if (UI.host && UI.host._sheet && UI.host._ist) { UI.host._ist.hidden = true; UI.host._ist.textContent = ""; }
  const stepperEl = $("#stepper");
  if (stepperEl) { stepperEl.hidden = true; stepperEl.textContent = ""; stepperEl._sx = null; }
  const bands = $("#bands");
  if (bands) bands.hidden = false;
  const was = UI.field;
  UI.field = null; UI.host = null;
  if (was && !quiet) render();
  return !!was;
}
export const stepperOpen = () => !!UI.field;
// DELAY / LO-CUT relocked (LOCK, the box's idle relock, a refusal): their steps close
onRelocked(() => { if (UI.field && (fieldOf(pathOf(UI.field)) || {}).lock) closeStepper(); });

function keysFor(tk, fd) {
  const st = Array.isArray(fd.stepper) && fd.stepper.length ? fd.stepper.slice() : [-1, 1];
  if (tk === "gain") {                          // FLAT in the middle: −3 · −½ · FLAT · +½ · +3
    const neg = st.filter((d) => d < 0), pos = st.filter((d) => d > 0);
    return [...neg, "flat", ...pos];
  }
  return st;
}
function buildSteps(tk, host, withLabel) {
  host.textContent = "";
  const path = pathOf(tk), fd = fieldOf(path);
  if (!fd) return;
  if (withLabel) {
    const lockKey = !!fd.lock;
    const sx = lockKey ? h("button", "sx lk", "<b></b><span>LOCK</span>") : h("button", "sx", "<b></b><span></span><i>✕</i>");
    sx.setAttribute("aria-label", lockKey ? "Lock DELAY and LO-CUT now" : "Close the steps");
    sx.addEventListener("click", () => (lockKey ? relock(true) : closeStepper()));
    host.append(sx);
    host._sx = sx;
  }
  const keys = keysFor(tk, fd);
  for (const d of keys) {
    if (d === "flat") { host.append(flatKey()); continue; }
    const b = h("button", "", "<span></span><small></small>");
    b._d = d;
    b.addEventListener("click", () => {
      const p = pathOf(tk);
      if (!editOk()) return;
      if (fd.lock && !alignOpen()) { lockedHint(p); return; }
      if (stepCmd(p, d) && !(UI.host && UI.host._sheet)) hudShow(tk, "");
      if (UI.field) armIdle();
      render();
    });
    host.append(b);
  }
  host.style.setProperty("--n", String(keys.length));
  renderStepper();
}
function flatKey() {
  const b = h("button", "flat", '<b class="fill"></b><span>FLAT</span>');
  b.setAttribute("aria-label", "Hold: this band flat and IN");
  bindHold(b, {
    ms: 600, delay: 60,
    begin: () => (linked() ? { s: UI.sel } : (toast("NO LINK · nothing is sent", "red"), null)),
    fire: (c) => {
      if (!linked()) { toast("LINK LOST · nothing sent", "red"); hap("fail"); return; }
      if (!editOk()) return;
      const wasOut = !(band(c.s) || {}).on;
      hap("click");
      request({ t: "band", s: c.s, g: 0, on: true }, (ok, r) => {
        if (ok) toast(shortName(c.s) + " FLAT" + (wasOut ? " · IN" : ""));
        else if (r.code !== "LINK") { toast(String(r.why || "refused"), "red"); hap("reject"); }
        render();
      }, 2500);
      hudShow("gain", "");
      if (UI.field) armIdle();
    },
    early: () => earlyFlash(b, "hold"),
    lost: () => { toast("LINK LOST · nothing sent", "red", 2600); hap("fail"); },
  });
  return b;
}

// The labels follow the band (a dot tap changes the band and keeps the field) and the shelf / bell.
export function renderStepper() {
  if (!UI.field) return;
  const tk = UI.field, path = pathOf(tk), fd = fieldOf(path);
  const host = UI.host && UI.host._sheet ? UI.host._ist : $("#stepper");
  if (!host || !fd) return;
  if (host._sx) {
    setText(host._sx.querySelector("b"), UI.host ? tileLabel(UI.host) : tk.toUpperCase());
    if (!fd.lock) {
      const b = band(UI.sel);
      setText(host._sx.querySelector("span"), ["freq", "gain", "q"].includes(tk) ? shortName(UI.sel) + (b && !b.on ? " OUT" : "") : fd.unit);
    }
  }
  const shelf = tk === "q" && bandKind(UI.sel) !== "bell";
  for (const b of host.children) {
    if (b._d == null) continue;
    let [big, small] = stepLabel(fd, b._d, { q: tk === "q", shelf });
    if (tk === "bal" && Math.abs(b._d) === Math.max(...fd.stepper.map(Math.abs))) small = b._d < 0 ? "tops" : "subs";
    setText(b.firstChild, big);
    setText(b.lastChild, small);
    b.lastChild.hidden = !small;
  }
  if (UI.host) renderTile(UI.host);
}
