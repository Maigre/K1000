// SPDX-License-Identifier: GPL-3.0-or-later
/* The sheets: SETUP (the D-013 groups, built from hello.setup: a new box row needs no page
   change), STATUS (every alert and pill) and THIS PHONE (this phone's own settings). A sheet
   stops above the footer: MASTER, the meters and MUTE stay live (DESIGN §6.1).

   SETUP rows (DESIGN §6.3, docs/14 §3.1.3, §4.6, §4.7):
   - num: a value tile (tap = steps under the row, slide = like the knob); Trim L / R side by side;
   - bool / choice: CHOOSE, THEN SET. A tap on another choice marks it (the row turns red,
     NOW → CHOSEN) and a red SET key appears on its own line below, never where the chosen key
     is; SET refuses a tap within 400 ms of the choice (a double-tap never commits), the choice
     expires after pick_window_s or when another row is touched. SET sends `choose {f, v, seen}`
     (seen = the value the row showed): a value changed meanwhile is never overwritten blind.
     A `rebuild` row's SET is a 1 s hold, marked MAY CLICK;
   - action: a 1 s hold (ALL FLAT…, UNDO, laptop access OPEN); laptop CLOSE is a tap;
   - page: read-only with the box's reason, or not shown (`hidden`). */

import { K, UI, linked, render, savePrefs, shortName } from "./store.js";
import { bindHold, earlyFlash } from "./hold.js";
import { request, BUILD } from "./link.js";
import { hap, hapLog } from "./haptics.js";
import { alerts, editOk, muteWho, pillList, toast, layout } from "./view.js";
import { forgetSheetTiles, makeTile, renderTile } from "./tile.js";
import { closeStepper } from "./stepper.js";
import { can, hasNative, nativeSend, refused as refusedNative, requestDiag } from "./native.js";
import { syncSub } from "./rta.js";
import { M as MIC, allowFromSetup, allowed as micAllowed, running as micRunning, setMic } from "./mic.js";
import { closePop } from "./graph.js";
import { $, $$, clamp, fmtDb, fmtHz, h, mmss, now, num, setText } from "./util.js";

const SET_GUARD_MS = 400;
const ME = "THIS PHONE";
let upd = [];
let inMeter = null;
let justSet = 0;
let stateAt = 0;
export function stateArrived() { stateAt = now(); }
const left = (s) => (num(s) ? Math.max(0, s - (now() - stateAt) / 1000) : 0);

// ------------------------------------------------------------------ open / close
export function openSheet(k) {
  if (UI.sheet === k) { closeSheet(); return; }
  closeStepper(true);
  closePop();                       // never left open under the sheet (Back would close it unseen)
  UI.sheet = k; UI.pick = null;
  if (k === "setup" && !UI.tab) UI.tab = groups()[0] || ME;
  $("#sheet").hidden = false;
  buildSheet();
  layout();
  syncSub();
  render();
}
export function closeSheet() {
  if (!UI.sheet) return false;
  if (UI.host && UI.host._sheet) closeStepper(true);
  UI.sheet = null; UI.pick = null; inMeter = null; upd = [];
  $("#sheet").hidden = true;
  $("#shBody").textContent = "";
  forgetSheetTiles();
  layout();
  syncSub();
  render();
  return true;
}
// Back: a pending choice first, then the sheet
export function backInSheet() {
  if (!UI.sheet) return false;
  if (UI.pick) { UI.pick = null; render(); return true; }
  return closeSheet();
}
const groups = () => (K.hello && Array.isArray(K.hello.setup) ? K.hello.setup.map((g) => String(g.group)) : []);
const tabsKey = () => groups().concat([ME]).join("|");      // the one key, built and compared

function buildTabs() {
  const el = $("#shTabs");
  const tabs = groups().concat([ME]);
  const key = tabsKey();
  if (el._k !== key) {
    el._k = key;
    el.textContent = "";
    el.style.setProperty("--tabs", String(tabs.length));
    for (const t of tabs) {
      const b = h("button", t === ME ? "me" : "");
      b.setAttribute("role", "tab");
      b.dataset.k = t;
      const words = t.split(" ");
      words.forEach((w, i) => { if (i) b.append(document.createElement("br")); b.append(document.createTextNode(w)); });
      b.addEventListener("click", () => {
        if (UI.tab === t) return;
        UI.tab = t; UI.pick = null; closeStepper(true); buildSheet(); render();
      });
      el.append(b);
    }
  }
  for (const b of $$("button", el)) b.setAttribute("aria-selected", String(b.dataset.k === UI.tab));
}

export function buildSheet() {
  upd = []; inMeter = null;
  forgetSheetTiles();
  const body = $("#shBody");
  body.textContent = "";
  setText($("#shTitle"), UI.sheet === "setup" ? "SETUP" : "STATUS");
  $("#shTabs").hidden = UI.sheet !== "setup";
  if (UI.sheet === "setup") {
    buildTabs();
    if (UI.tab === ME) buildMe();
    else buildGroup(UI.tab);
  } else buildStatus();
  body.scrollTop = 0;
  renderSheet();
}
export function renderSheet() {
  if (!UI.sheet) return;
  if (UI.sheet === "setup" && $("#shTabs")._k !== tabsKey()) {      // the box's groups changed
    if (!groups().includes(UI.tab) && UI.tab !== ME) UI.tab = groups()[0] || ME;
    buildSheet();
    return;
  }
  if (UI.pick && now() > UI.pick.until) UI.pick = null;
  for (const f of upd) f();
}

// ------------------------------------------------------------------ row kinds
function srow(id) { const r = h("div", "srow"); if (id) r.dataset.row = id; $("#shBody").append(r); return r; }
function head(r, label) {
  const hd = h("div", "rh", '<span class="rl"></span><span class="rv"></span>');
  setText(hd.firstChild, label);
  r.append(hd);
  return hd.lastChild;
}
function note(r) { const n = h("div", "rn"); r.append(n); return n; }

function rowNum(label, items, extra) {
  const r = srow(items.map((it) => it.f).join(","));
  const rv = head(r, label);
  const ist = h("div", "istep");
  ist.hidden = true;
  const box = items.length > 1 ? h("div", "row2") : r;
  for (const it of items) {
    const t = makeTile(it.f, { sheet: true, label: it.tile });
    t._ist = ist;
    box.append(t);
    upd.push(() => renderTile(t));
  }
  if (box !== r) r.append(box);
  r.append(ist);
  const n = note(r);
  upd.push(() => { setText(rv, ""); setText(n, extra ? extra() : ""); });
}
function rowInfo(label, value, nt) {
  const r = srow();
  const rv = head(r, label);
  const n = note(r);
  upd.push(() => { setText(rv, typeof value === "function" ? value() : value); setText(n, typeof nt === "function" ? nt() : nt || ""); });
}
const upper = (s) => String(s).toUpperCase();
function choiceName(row, v) {
  const i = (row.choices || []).findIndex((c) => c === v);
  return i >= 0 && row.names && row.names[i] != null ? upper(row.names[i]) : v == null ? "--" : upper(v);
}
function rowChoice(group, row, tileRow, tileLabel) {
  const f = row.f;
  const r = srow(f);
  const rv = head(r, row.label);
  const seg = h("div", "seg");
  const btns = (row.choices || []).map((v) => {
    const b = h("button");
    b.textContent = choiceName(row, v);
    b.addEventListener("click", () => { if (choiceTap(row, v)) reveal(r); });
    seg.append(b);
    return [v, b];
  });
  const setRow = h("div", "setrow");
  const setNote = h("div", "rn");
  const setB = h("button", "setk" + (row.rebuild ? " rb" : ""), '<b class="fill"></b><span></span>');
  if (row.rebuild) {
    bindHold(setB, {
      ms: 1000, delay: 0,
      begin: () => (setReady(row) ? {} : null),
      fire: () => commit(group, row),
      early: () => toast("HOLD 1 s TO SET · MAY CLICK"),
    });
  } else setB.addEventListener("click", () => { if (setReady(row)) commit(group, row); });
  setRow.append(setNote, setB);
  setRow.hidden = true;
  if (tileRow) {
    const pair = h("div", "row2");
    const t = makeTile(tileRow.f, { sheet: true, label: tileLabel });
    const ist = h("div", "istep");
    ist.hidden = true;
    t._ist = ist;
    pair.append(seg, t);
    r.append(pair, ist);
    upd.push(() => renderTile(t));
  } else r.append(seg);
  const n = note(r);
  const bar = h("div", "pbar", "<b></b>");
  r.append(setRow, bar);
  upd.push(() => {
    const S = K.state;
    const cur = S && S.setup ? S.setup[f] : null;
    const ro = S && S.readonly ? S.readonly[f] : null;
    const p = UI.pick && UI.pick.f === f ? UI.pick : null;
    r.classList.toggle("ro", !!ro);
    r.classList.toggle("pick", !!p);
    let nowTxt = choiceName(row, cur);
    if (f === "lowcut_on" && cur === true && S && num(S.lowcut_hz)) nowTxt += " · " + Math.round(S.lowcut_hz) + " Hz";
    if (ro) nowTxt += " · READ-ONLY";
    setText(rv, p ? choiceName(row, cur) + " → " + choiceName(row, p.v) : nowTxt);
    for (const [v, b] of btns) {
      b.setAttribute("aria-pressed", String(v === cur && !ro && !p));
      b.classList.toggle("chosen", !!p && p.v === v);
    }
    setRow.hidden = !p;
    if (p) {
      setText(setNote, row.rebuild ? "Rebuilds the pipeline: it may click." : "Nothing changes until SET.");
      if (setB._early == null) setText(setB.querySelector("span"), row.rebuild ? "HOLD 1 s · SET" : "SET " + choiceName(row, p.v));
      setB.setAttribute("aria-label", "Set " + row.label + " to " + choiceName(row, p.v) + (row.rebuild ? ": hold 1 second, it may click" : ""));
    }
    setText(n, ro ? String(ro) : f === "lowcut_on" && !p ? "Its frequency is the LO-CUT tile on LIVE (locked)." : f === "mono_subs" && !p ? "Keep ON: out 3 feeds both subs (D-012)." : "");
    const pw = (K.hello && num(K.hello.pick_window_s) ? K.hello.pick_window_s : 8) * 1000;
    bar.firstChild.style.width = p ? (clamp((p.until - now()) / pw, 0, 1) * 100).toFixed(1) + "%" : "0";
  });
}
function setReady(row) {
  const p = UI.pick;
  if (!p || p.f !== row.f) return false;
  if (now() - p.t < SET_GUARD_MS) { hap("reject"); return false; }     // a double-tap: refused
  return editOk();
}
// a choice started: true (its SET line is then scrolled into view, reveal)
function choiceTap(row, v) {
  if (now() - justSet < 450) return false;
  const S = K.state;
  const ro = S && S.readonly ? S.readonly[row.f] : null;
  if (ro) { toast(String(ro)); hap("reject"); return false; }
  if (!linked()) { toast("NO LINK · nothing is sent", "red"); return false; }
  const cur = S.setup ? S.setup[row.f] : null;
  const p = UI.pick && UI.pick.f === row.f ? UI.pick : null;
  if (p && p.v === v) { toast(row.rebuild ? "HOLD THE RED SET KEY BELOW" : "TAP THE RED SET KEY BELOW"); return false; }   // the chosen key never sets
  if (v === cur) { UI.pick = null; if (!p) toast("ALREADY " + choiceName(row, v)); render(); return false; }
  const pw = (K.hello && num(K.hello.pick_window_s) ? K.hello.pick_window_s : 8) * 1000;
  UI.pick = { f: row.f, v, t: now(), until: now() + pw, seen: cur };
  hap("tick");
  render();
  return true;
}
// The pending choice's SET line, below the fold on a short screen: the sheet scrolls just enough
// to show the whole row (its own scroll only: the page itself never moves)
function reveal(r) {
  renderSheet();                         // the SET line shows now, not at the next frame
  const body = $("#shBody");
  const rb = r.getBoundingClientRect(), bb = body.getBoundingClientRect();
  if (rb.bottom > bb.bottom - 4) body.scrollTop += Math.min(rb.bottom - bb.bottom + 8, rb.top - bb.top);
}
function commit(group, row) {
  const p = UI.pick;
  if (!p || p.f !== row.f) return;
  if (!editOk()) return;                 // a stalled link: said; the choice stays until it expires
  UI.pick = null;
  justSet = now();
  const id = request({ t: "choose", f: row.f, v: p.v, seen: p.seen }, (ok, r) => {
    if (ok) { hap("ok"); toast(group + " › " + row.label + ": " + choiceName(row, r.v != null ? r.v : p.v) + (row.rebuild ? " · pipeline rebuilt" : ""), "white"); }
    else if (r.code === "STALE") { toast("CHANGED MEANWHILE · nothing written", "red"); hap("reject"); }
    else if (r.code === "NOCHANGE") { toast("ALREADY SET"); }
    else if (r.code === "LINK") { toast("LINK LOST · not confirmed: look again", "red"); hap("fail"); }
    else if (r.code === "TIMEOUT") { toast("No answer from K1000: look again", "red"); hap("fail"); }
    else { toast(String(r.why || "refused"), "red"); hap("reject"); }
    render();
  }, 6000);
  if (id == null) toast("NO LINK · nothing is sent", "red");
  render();
}
// a big action: 1 s hold, never a modal
function rowAction(o) {
  const r = srow();
  const rv = head(r, o.label);
  const b = h("button", "act " + (o.cls || ""), '<b class="fill"></b><span></span>');
  bindHold(b, {
    ms: 1000, delay: 0,
    begin: () => (o.holdable() && linked() ? {} : null),
    fire: () => { justSet = now(); o.fire(); render(); },
    early: () => earlyFlash(b, "hold 1 s", 700, render),
  });
  if (o.tap) b.addEventListener("click", () => { if (now() - justSet > 450 && !o.holdable() && o.tapable()) { o.tap(); render(); } });
  const n = note(r);
  r.append(b);
  r.append(n);
  upd.push(() => {
    if (b._early == null) setText(b.querySelector("span"), o.text());
    b.disabled = !!(o.disabled && o.disabled());
    setText(rv, o.value ? o.value() : "");
    setText(n, o.note());
  });
}
function act(a, done) {
  if (!editOk()) return;
  request({ t: "act", a }, (ok, r) => {
    if (ok) hap("ok");
    else if (r.code === "LINK") { toast("LINK LOST · not confirmed: look again", "red"); hap("fail"); }
    else if (r.code === "TIMEOUT") { toast("No answer from K1000: look again", "red"); hap("fail"); }
    else { toast(String(r.why || "refused"), "red"); hap("reject"); }
    if (done) done(ok, r);
    render();
  }, 5000);
}
function rowSeg(label, get, choices, set, nt) {
  const r = srow();
  head(r, label);
  const seg = h("div", "seg");
  const btns = choices.map(([v, txt]) => {
    const b = h("button");
    b.textContent = txt;
    b.addEventListener("click", () => { set(v); hap("tick"); render(); });
    seg.append(b);
    return [v, b];
  });
  r.append(seg);
  const n = note(r);
  setText(n, nt || "");
  upd.push(() => { const c = get(); for (const [v, b] of btns) b.setAttribute("aria-pressed", String(v === c)); });
}
// the capture peak per input channel, before the trim (m.i), and the box's clip latch
function rowInMeter() {
  const r = srow();
  const rv = head(r, "Input level");
  const m = h("div", "inm", '<div class="inbars"><div class="inb"><span>L</span><div class="bar"><i></i></div></div>' +
    '<div class="inb"><span>R</span><div class="bar"><i></i></div></div><div class="inscale"><span class="s50">−24</span>' +
    '<span class="s75">−12</span><span class="s875">−6</span><span class="s100">0</span></div></div><div class="inclip">CLIP</div>');
  r.append(m);
  const n = note(r);
  setText(n, "Capture peak, before the trim. Keep the source under −6 dBFS: the trim cannot undo a clip at the input.");
  inMeter = { bars: $$(".bar i", m), rv, clip: m.querySelector(".inclip") };
  upd.push(() => inMeter && inMeter.clip.classList.toggle("on", !!(K.state && K.state.in_clip)));
}
export function drawInMeter() {
  if (!inMeter || !K.meters || !Array.isArray(K.meters.i)) return;
  const v = K.meters.i;
  inMeter.bars.forEach((b, i) => { b.style.width = ((1 - clamp(((num(v[i]) ? v[i] : -60) + 48) / 48, 0, 1)) * 100).toFixed(1) + "%"; });
  setText(inMeter.rv, v.slice(0, 2).map((x) => fmtDb(x)).join(" / ") + " dBFS");
}

// ------------------------------------------------------------------ SETUP › a box group
// what a tile says in its group: the side's own words (the prototype's TOP L / SUB R, SUB LP)
const SIDE = { TOPS: "TOP", SUBS: "SUB" };
const TILE_LABEL = { sub_lowpass_hz: "SUB LP" };
function tileWord(group, it) {
  if (TILE_LABEL[it.f]) return TILE_LABEL[it.f];
  const lr = /\b(L|R)$/.exec(String(it.label));
  if (SIDE[group] && lr) return SIDE[group] + " " + lr[1];
  return upper(it.label === "Trim" ? group + " trim" : it.label);
}
function buildGroup(name) {
  const g = (K.hello && K.hello.setup || []).find((x) => String(x.group) === name);
  if (!g) return;
  const items = (g.items || []).filter((it) => it && it.phone !== "hidden");
  if (name === "INPUT") rowInMeter();
  for (let i = 0; i < items.length; i++) {
    const it = items[i], nx = items[i + 1];
    if (it.kind === "num") {
      if (it.phone === "readonly") { rowInfo(it.label, () => String(K.state && K.state.setup ? K.state.setup[it.f] : "--"), it.why || ""); continue; }
      const pair = nx && nx.kind === "num" && nx.field === it.field && nx.phone === "edit" && /\bL$/.test(it.label) && /\bR$/.test(nx.label);
      if (pair) {
        rowNum(it.label.replace(/\s*L$/, "") + "s", [{ f: it.f, tile: tileWord(name, it) }, { f: nx.f, tile: tileWord(name, nx) }]);
        i++;
      } else rowNum(it.label, [{ f: it.f, tile: tileWord(name, it) }]);
    } else if (it.kind === "bool" || it.kind === "choice") {
      const tileRow = nx && nx.kind === "num" && nx.phone === "edit" && String(nx.label).startsWith(it.label) ? nx : null;
      rowChoice(name, it, tileRow, tileRow ? (TILE_LABEL[tileRow.f] || upper(String(tileRow.label).replace(it.label, "").trim() || tileRow.label)) : "");
      if (tileRow) i++;
    } else if (it.kind === "action") {
      actionRow(it);
    } else if (it.kind === "page") {
      pageRow(it);
    }
    // a kind this page does not know: ignored (docs/14 §3.1.3)
  }
  if (name === "LIVE EQ") rowInfo("Spectrum source", "PGM", "OUT and BOX MIC after the gig: new box work.");
  if (name === "PHONE") rowInfo("Phones", () => (K.state ? K.state.phones : 0) + " connected", "Up to " + ((K.hello && K.hello.limits && K.hello.limits.max_phones) || 4) + ". Values are shared; the selection and the ALIGN unlock are per phone.");
  if (name === "BOX") {
    const sf = K.hello && K.hello.surface || {};
    rowInfo("Knobs", () => (sf.knobs === false ? "none (headless)" : K.state && K.state.knobs_ok === false ? "OFFLINE" : K.state && K.state.knobs_ok === true ? "OK" : "--"),
      sf.knobs === false ? "No screen or knobs: this phone is the whole interface." : "B1 PAGE · B2 BAND/UNLOCK · B3 NOTCH · B4 MUTE");
    rowInfo("Box", () => (K.hello ? String(K.hello.server || "") + " · v2 " + String(K.hello.build || "") : "--"), "");
  }
}
// a read-only row: the box's words, "ON · SWITCHED AT THE BOX" = the value, then the note
function pageRow(it) {
  const parts = String(it.why || "").split(" · ");
  if (it.f === "PHONE") {
    const headless = !!(K.hello && K.hello.surface && K.hello.surface.knobs === false);
    rowInfo("Phone control", parts[0] || "--", headless ? "Headless: always on (a saved off is ignored)."
      : "Switched at the box only: a phone can never turn phones off.");
    return;
  }
  rowInfo(it.label, parts[0] || "", parts.slice(1).join(" · "));
}
function actionRow(it) {
  const acts = Array.isArray(it.act) ? it.act : [];
  if (it.f === "reset_live" || acts[0] === "reset_live") {
    rowAction({
      label: "All bands flat", cls: "warn",
      holdable: () => true, text: () => "ALL FLAT…",
      fire: () => act("reset_live", (ok, r) => { if (ok) toast(r.v && r.v.changed === false ? "ALREADY FLAT" : "ALL FLAT · undo below for " + mmss(r.v && num(r.v.undo_s) ? r.v.undo_s : 300), "white"); }),
      note: () => "Hold 1 s. The 4 bells and both shelves go to 0 dB and their default frequencies, all IN.",
    });
  } else if (it.f === "undo_reset" || acts[0] === "undo_reset") {
    const avail = () => left(K.state && K.state.undo_reset_s) > 0;
    rowAction({
      label: "Undo all flat",
      holdable: avail, disabled: () => !avail(),
      text: () => (avail() ? "UNDO · " + mmss(left(K.state.undo_reset_s)) : "NOTHING TO UNDO"),
      fire: () => act("undo_reset", (ok) => { if (ok) toast("LIVE EQ RESTORED", "white"); }),
      note: () => "Hold 1 s. Up to " + mmss((K.hello && K.hello.undo_reset_s) || 300) + " after ALL FLAT.",
    });
  } else if (acts.includes("laptop_open")) {
    const lap = () => (K.state && K.state.laptop) || { state: "off", s: 0 };
    const isOpen = () => ["open", "opening"].includes(lap().state);
    const host = location.hostname || "10.42.0.1";
    rowAction({
      label: it.label || "Laptop access", cls: "go",
      holdable: () => lap().state === "closed",
      tapable: isOpen,
      disabled: () => !["closed", "open", "opening"].includes(lap().state),
      text: () => (isOpen() ? "CLOSE" : lap().state === "closed" ? "OPEN · hold 1 s" : lap().state === "closing" ? "CLOSING…"
        : lap().state === "unavailable" ? "UNAVAILABLE" : "NOT ON THIS BOX"),
      value: () => (lap().state === "open" ? "OPEN " + mmss(left(lap().s)) : String(lap().state)),
      fire: () => act("laptop_open", (ok) => { if (ok) toast("LAPTOP ACCESS OPENING", "white"); }),
      tap: () => act("laptop_close", (ok) => { if (ok) { toast("LAPTOP ACCESS CLOSED"); hap("click"); } }),
      note: () => (isOpen() ? "CamillaGUI http://" + host + ":5005 · SSH " + host + " · system EQ: /tune"
        : "Opens CamillaGUI, SSH and the /tune page to a laptop on the K1000 Wi-Fi for 2 h."),
    });
  } else {
    rowInfo(it.label, "", "On the box.");
  }
}

// ------------------------------------------------------------------ SETUP › THIS PHONE
function buildMe() {
  rowSeg("Mute side", () => UI.prefs.side, [["left", "LEFT"], ["right", "RIGHT"]], (v) => { UI.prefs.side = v; savePrefs(); }, "The footer mirrors for a left hand.");
  rowSeg("Haptics", () => UI.prefs.haptics, [["off", "OFF"], ["light", "LIGHT"], ["full", "FULL"]], (v) => { UI.prefs.haptics = v; savePrefs(); },
    "LIGHT drops the tick per detent. The box's answers (mute, no answer) always play." + (can("haptic") ? "" : " Chrome: vibration only."));
  rowSeg("Slide sensitivity", () => UI.prefs.sens, [[0.75, "×0.75"], [1, "×1"], [1.5, "×1.5"]], (v) => { UI.prefs.sens = v; savePrefs(); },
    "×1 = 14 px a detent, about 2 mm. Never on master up (20 px a detent, always).");
  if (can("fullscreen")) {
    rowSeg("Fullscreen", () => UI.prefs.fullscreen || "on", [["off", "OFF"], ["on", "ON"]],
      (v) => { UI.prefs.fullscreen = v; savePrefs(); nativeSend({ t: "fullscreen", on: v === "on" }); }, "Hides the system bars: more graph, no shade pulled mid-slide.");
  }
  rowMic();
  rowGestures();
  // diagnostics: the APK's own (BRIDGE.md `diag`)
  const r = srow();
  const rv = head(r, "Diagnostics");
  if (can("diag")) {
    const two = h("div", "row2");
    const rd = h("button", "act"); rd.textContent = "READ";
    rd.addEventListener("click", () => { requestDiag(false); toast("Reading the app's diagnostics…"); });
    const op = h("button", "act"); op.textContent = "APP PANEL";
    op.addEventListener("click", () => requestDiag(true));
    two.append(rd, op);
    r.append(two);
  }
  const box = h("div", "");
  r.append(box);
  upd.push(() => {
    setText(rv, hasNative() ? "app " + UI.native.app : "browser");
    const d = UI.diag;
    const v = (x) => (x == null ? "--" : typeof x === "object" ? JSON.stringify(x) : String(x));
    const rows = d ? [["app", v(d.app) + " · " + v(d.build)], ["phone", v(d.model) + " · Android " + v(d.release) + " · SDK " + v(d.sdk)],
      ["WebView", v(d.webview_version)], ["raw mic", v(d.unprocessed_supported)], ["haptics", "vibrator " + v(d.has_vibrator) + " · touch feedback " + v(d.system_haptics)],
      ["exclusion", Array.isArray(d.exclusion_rects) ? d.exclusion_rects.length + " rects" : "--"], ["gesture insets", v(d.gesture_insets)],
      ["screen", v(d.viewport_css) + " · ×" + v(d.density)]]
      : [["page", "v2 " + BUILD], ["viewport", innerWidth + "×" + innerHeight + " · ×" + (window.devicePixelRatio || 1)],
        ["app", hasNative() ? "tap READ" : "none: Chrome (the APK adds haptics, fullscreen, Back and these diagnostics)"]];
    if (refusedNative.length) rows.push(["bridge", "handshake refused from " + refusedNative.join(", ")]);
    const key = JSON.stringify(rows);
    if (box._k === key) return;
    box._k = key;
    box.textContent = "";
    for (const [k, v] of rows) { const kv = h("div", "kv", "<span></span><span></span>"); setText(kv.firstChild, k); setText(kv.lastChild, v == null ? "--" : v); box.append(kv); }
  });
  // the field-test escape hatch: the v1 page
  const p = srow();
  head(p, "Page");
  if (can("set_ui")) {
    const b = h("button", "act go"); b.textContent = "OPEN UI v1";
    b.addEventListener("click", () => nativeSend({ t: "set_ui", ui: "v1" }));
    p.append(b);
  } else {
    const a = h("a", "act go linkbtn");
    a.href = "/"; a.textContent = "OPEN UI v1";
    p.append(a);
  }
  setText(note(p), "The escape hatch for the field test: the v1 page at https://10.42.0.1/ (MUTE, master, balance, the live EQ).");
}

// THIS PHONE › Phone mic: the ONE place the page asks for the mic (DESIGN §7, §9.15: a system
// dialog from LIVE would cover MUTE). ALLOW until it is allowed, then OFF / ON; it says what the
// browser kept of the processing the page asked off (echo cancellation, noise suppression, auto gain).
const PROC = [["echoCancellation", "echo cancel"], ["noiseSuppression", "noise suppr."], ["autoGainControl", "auto gain"]];
function procWords() {
  const st = MIC.settings;
  if (!st) return "Asked with echo cancel, noise suppression and auto gain OFF; what the browser kept shows after ALLOW.";
  const kept = PROC.filter(([k]) => st[k] === true).map(([, w]) => w);
  const rate = num(st.sampleRate) ? " · " + (st.sampleRate / 1000).toFixed(1).replace(/\.0$/, "") + " kHz" : "";
  return (kept.length ? "The browser KEPT " + kept.join(", ").toUpperCase() + " ON (the line may breathe or gate): " : "Processing asked OFF, read back: ") +
    PROC.map(([k, w]) => w + " " + (st[k] === true ? "ON" : st[k] === false ? "OFF" : "?")).join(" · ") + rate + ".";
}
function rowMic() {
  const r = srow("phone_mic");
  const rv = head(r, "Phone mic");
  const ask = h("button", "act go", "<span></span>");
  ask.addEventListener("click", () => { allowFromSetup(); hap("tick"); render(); });
  const seg = h("div", "seg");
  const btns = [["off", "OFF"], ["on", "ON"]].map(([v, t]) => {
    const b = h("button");
    b.textContent = t;
    b.addEventListener("click", () => { setMic(v === "on"); hap("tick"); render(); });
    seg.append(b);
    return [v, b];
  });
  const n = note(r), n2 = note(r);
  r.append(ask, seg, n, n2);
  upd.push(() => {
    const a = micAllowed();
    ask.hidden = a;
    seg.hidden = !a;
    setText(ask.querySelector("span"), MIC.state === "starting" ? "ASKING…" : MIC.perm === "denied" ? "TRY AGAIN" : "ALLOW THE MIC");
    ask.disabled = MIC.perm === "unsupported" || MIC.state === "starting";
    for (const [v, b] of btns) b.setAttribute("aria-pressed", String(v === (UI.prefs.mic === "off" ? "off" : "on")));
    setText(rv, !a ? (MIC.perm === "denied" ? "BLOCKED" : MIC.perm === "unsupported" ? "UNAVAILABLE" : "NOT ALLOWED YET")
      : UI.prefs.mic === "off" ? "OFF" : micRunning() ? "ON · LISTENING" : "ON");
    setText(n, (MIC.err ? MIC.err + ". " : "") + (a ? "ON: the cyan line (PGM ▾ › PHONE MIC or BOTH) and MEASURE (≋). It listens only while it is shown." :
      "Asked here only, never from LIVE (the dialog would cover MUTE). Then PGM ▾ › PHONE MIC, or ≋ MEASURE."));
    setText(n2, procWords() + " Audio stays on this phone: nothing is sent.");
  });
}

// the prototype's "?" legend, kept here: how every gesture on LIVE works
const GESTURES = [
  ["Tap a value", "its steps open in the band row (✕, a tap on the graph, Back or 8 s closes them)"],
  ["Slide ◀ ▶", "on any value, like a knob: slow = fine steps, quick = bigger; master up is 0.5 dB per 20 px, never faster"],
  ["Band", "tap = select · hold 0.6 s = band IN / OUT (kept, not acting; FLAT brings it back IN) · 3 quick taps on the selected chip = FLAT (UNDO 5 s) · moving 12 px cancels"],
  ["Graph", "drag a dot · long-press = notch helper (silent bell, Q 8.65, then cut) · hold ▼ PEAK = notch at the peak (the mic's own PEAK when its line is shown)"],
  ["≋", "MEASURE: the phone mic and RESPONSE (walk the room) in the ALIGN row's place · ≋ or Back leaves"],
  ["ALIGN", "hold DELAY or LO-CUT 0.8 s to unlock both for 30 s · LOCK in their steps relocks"],
  ["MUTE", "hold 0.6 s · unmute: hold 1.2 s · moving 12 px cancels · works while another key is held"],
  ["SETUP", "tap a choice, then the red SET key below it"],
  ["2 fingers", "the second finger stops a slide, a drag or a hold (never MUTE's)"],
];
function rowGestures() {
  const r = srow();
  head(r, "Gestures");
  const dl = h("dl", "gl");
  for (const [k, v] of GESTURES) {
    const dt = h("dt"), dd = h("dd");
    dt.textContent = k; dd.textContent = v;
    dl.append(dt, dd);
  }
  r.append(dl);
}

// ------------------------------------------------------------------ STATUS
function buildStatus() {
  const body = $("#shBody");
  const box = h("div", "");
  body.append(box);
  upd.push(() => {
    const a = alerts();
    const key = JSON.stringify(a);
    if (box._k === key) return;
    box._k = key;
    box.textContent = "";
    if (!a.length) { const d = h("div", "al"); d.textContent = "No alerts"; box.append(d); }
    for (const [lvl, t] of a) { const d = h("div", "al " + lvl), sp = h("span"); sp.textContent = t; d.append(sp); box.append(d); }
  });
  const grp = (t) => { const g = h("div", "grp"); g.textContent = t; body.append(g); };
  const kv = (k, v) => {
    const r = h("div", "kv", "<span></span><span></span>");
    setText(r.firstChild, k);
    body.append(r);
    upd.push(() => setText(r.lastChild, v()));
  };
  const S = () => K.state || {};
  grp("AUDIO");
  kv("Mute", () => (S().muted ? "MUTED" + (muteWho() ? " · " + muteWho() : "") + (S().mute_band && S().mute_band.text ? " · " + S().mute_band.text : "") : "open") + (S().mute_known === false ? " (unknown: saved value)" : ""));
  kv("Engine", () => (linked() ? String(S().engine || "--") + " · load " + (num(S().load) ? S().load.toFixed(1) + " %" : "--") + (S().ready ? "" : " · syncing") : "— (no link)"));
  kv("Device", () => String(S().device || "--") + (S().device_present === false ? " · NOT FOUND" : ""));
  kv("Profile", () => String(S().profile || "--") + (S().readonly && S().readonly.profile ? " · " + S().readonly.profile : ""));
  kv("Input clip", () => (S().in_clip ? "CLIPPED (latched 3 s)" : "none"));
  kv("Spectrum", () => (S().spectrum ? (S().spectrum.on ? "ON" : "OFF") + " · " + String(S().spectrum.src || "--") + " · " +
    (S().spectrum.state === "off" ? "not running (no screen or phone watching)" : String(S().spectrum.state || "--")) : "--"));
  kv("Mono subs · sub LP", () => {
    const u = S().setup || {};
    return (u.mono_subs === true ? "ON" : u.mono_subs === false ? "OFF" : "--") + " · " +
      (u.sub_lowpass_on === false ? "OFF" : num(u.sub_lowpass_hz) ? fmtHz(u.sub_lowpass_hz) + " Hz" : "--");
  });
  kv("Phone mic", () => (!micAllowed() ? (MIC.perm === "denied" ? "blocked" : "not allowed (SETUP › THIS PHONE)") : UI.prefs.mic === "off" ? "off"
    : micRunning() ? "listening · " + (MIC.rate / 1000).toFixed(1) + " kHz" : "on, not shown now") + (MIC.err ? " · " + MIC.err : ""));
  kv("On the graph", () => pillList().map((x) => x[1]).join(" · ") || "no pills");
  kv("Box says", () => String(S().hint || "") || "—");
  grp("LINK");
  kv("Phones", () => (num(S().phones) ? S().phones : 0) + (K.hello ? " · this is phone " + K.hello.you : "") + (num(K.rtt) ? " · round trip " + Math.round(K.rtt) + " ms" : ""));
  kv("Wi-Fi", () => "K1000 access point · " + (location.hostname || "10.42.0.1"));
  kv("Knobs", () => (K.hello && K.hello.surface && K.hello.surface.knobs === false ? "none (headless)" : S().knobs_ok === false ? "OFFLINE" : S().knobs_ok ? "OK" : "--"));
  kv("Box page", () => String(S().box_page || "--") + " · knobs on " + (Number.isInteger(S().box_band) && K.hello ? shortName(S().box_band) : "--"));
  kv("Laptop access", () => (S().laptop && S().laptop.state === "open" ? "open · " + mmss(left(S().laptop.s)) : String((S().laptop || {}).state || "--")));
  kv("Undo all flat", () => (left(S().undo_reset_s) > 0 ? mmss(left(S().undo_reset_s)) : "—"));
  grp("BUILDS");
  kv("Box", () => (K.hello ? String(K.hello.server || "--") + " · v2 build " + String(K.hello.build || "--") : "--"));
  kv("Page", () => "v2 " + BUILD);
  kv("App", () => (hasNative() ? "K1000 app " + UI.native.app + (Number.isInteger(UI.native.code) ? " · code " + UI.native.code : "") + " (" + UI.native.build + ")" +
    (K.appCheck && K.appCheck.app_ok === false ? " · BOX WANTS code " + K.appCheck.min_app + "+" : "") : "browser"));
  kv("Haptics sent", () => hapLog.slice(-6).join(" ") || "—");
}

export function initSheets() {
  $("#setupBtn").addEventListener("click", () => openSheet("setup"));
  $("#stBtn").addEventListener("click", () => openSheet("status"));
  $("#alertLine").addEventListener("click", () => openSheet("status"));
  $("#shX").addEventListener("click", closeSheet);
  $("#shBody").addEventListener("pointerdown", (e) => {
    // touching another row cancels a pending choice
    if (UI.pick && !e.target.closest('[data-row="' + CSS.escape(UI.pick.f) + '"]')) { UI.pick = null; render(); }
  }, true);
}
