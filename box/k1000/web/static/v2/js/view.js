// SPDX-License-Identifier: GPL-3.0-or-later
/* The page around the controls: the status strip, the alert line, the overlay strip's pills,
   the meters, toasts, the muted frame and the layout of the overlays (sheets and toasts stop
   above the footer: MASTER, the meters and MUTE stay live in every view, DESIGN §9.1). */

import { K, UI, canEdit, fallback, holdUnmutes, linkSlow, linked } from "./store.js";
import { reloadState } from "./link.js";
import { hap } from "./haptics.js";
import { $, $$, clamp, h, mmss, now, num, setText } from "./util.js";

// ------------------------------------------------------------------ toast
let toastT = 0, toastAt = 0;
export const toastLog = [];
export const lastToastAge = () => (toastAt ? now() - toastAt : Infinity);
export function toast(msg, kind, ms) {
  toastAt = now();
  const el = $("#toast");
  if (!el) return;
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = String(msg);
  el.hidden = false;
  toastLog.push(String(msg));
  if (toastLog.length > 40) toastLog.shift();
  clearTimeout(toastT);
  toastT = setTimeout(() => { el.hidden = true; }, ms || 1900);
}

// May an edit go out now? Not without a link, nor into a link silent for store.SLOW_MS (TCP
// would deliver it late, in a burst): refused and said. MUTE never asks (mute.js).
export function editOk(quiet) {
  if (canEdit()) return true;
  if (!quiet) { toast(linked() ? "LINK SLOW · nothing sent" : "NO LINK · nothing is sent", "red"); hap("reject"); }
  return false;
}

// ------------------------------------------------------------------ alerts (box + page)
// [level, text, own]: `own` = the page's own words, written to fit the 28 px alert line at
// 360 px with the action first (the lab's fit test fails if one is cut); the box's alerts may
// end in an ellipsis there (STATUS lists them whole).
const LEVEL = { critical: "crit", warning: "warn", info: "info" };
export function alerts() {
  const a = [];
  const S = K.state, H = K.hello;
  const fb = fallback();
  // between a retry's open and its first state, the last bad state stands
  const L = K.link === "ok" && !K.fresh && K.lastBad ? K.lastBad : K.link;
  if (L === "old") a.push(["crit", reloadState.behind ? "OLD PAGE · close K1000, open it again" : "OLD PAGE · reloading…", 1]);
  else if (L === "off") a.push(["crit", "PHONE CONTROL OFF · " + fb, 1]);
  else if (L === "busy") a.push(["crit", "K1000 HAS " + ((H && H.limits && H.limits.max_phones) || 4) + " PHONES · close one", 1]);
  else if (L === "nolink") a.push(["crit", "NO LINK · to mute: " + fb, 1]);
  else if (L === "connecting") a.push(["info", "CONNECTING TO K1000…", 1]);
  if (reloadState.outdated) a.push(["info", "K1000 WAS UPDATED · the page reloads when no finger is down", 1]);
  else if (reloadState.behind && L !== "old") a.push(["info", "K1000 WAS UPDATED · close K1000 and open it again", 1]);
  if (linked() && S) {
    if (linkSlow()) a.push(["crit", "LINK SLOW · edits paused · MUTE still sends", 1]);
    if (S.muted && !holdUnmutes()) {
      // the instruction first: at 360 px the end of the line may not show
      const t = S.mute_band && S.mute_band.text ? String(S.mute_band.text) : "";
      a.push(["warn", "HOLD = KEEP MUTED · K1000 " + (t ? t : "HOLDS MUTE"), 1]);
    }
    for (const x of Array.isArray(S.alerts) ? S.alerts : []) {
      if (Array.isArray(x) && x.length >= 2) a.push([LEVEL[x[0]] || "warn", String(x[1]), 0]);
    }
    if (S.in_clip && !a.some((x) => /CLIP/.test(x[1]))) a.push(["crit", "INPUT CLIP · turn the source down", 1]);
    if (num(S.phones) && S.phones > 1) a.push(["info", S.phones + " PHONES · the last change wins", 1]);
  }
  const rank = { crit: 0, warn: 1, info: 2 };
  return a.map((x, i) => [x[0], x[1], i, x[2]]).sort((p, q) => rank[p[0]] - rank[q[0]] || p[2] - q[2]).map((x) => [x[0], x[1], x[3]]);
}

// ------------------------------------------------------------------ pills
// safety first (POL INV, LO-CUT OFF), then an open laptop access, the bands OUT, the rest; what
// does not fit folds into "+n" from the end (STATUS lists them all)
export function pillList() {
  const S = K.state;
  const box = S && Array.isArray(S.pills) ? S.pills.map(String) : [];
  const rank = (p) => (/^POL INV/.test(p) ? 0 : /^LO-CUT OFF/.test(p) ? 1 : /^LAPTOP/.test(p) ? 2 : /^OUT /.test(p) ? 3 : 4);
  const cls = (p) => (rank(p) <= 1 ? "warn" : rank(p) === 3 ? "out" : "");
  const out = box.map((p, i) => [rank(p), i, p]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map((x) => [cls(x[2]), x[2]]);
  if (UI.freeze) out.push(["cy", "FROZEN"]);
  return out;
}
function fitPills(el) {
  const kids = Array.from(el.children), more = kids.pop();
  if (!more) return;
  kids.forEach((k) => { k.hidden = false; });
  more.hidden = true;
  if (el.scrollWidth <= el.clientWidth + 0.5) return;
  more.hidden = false;
  for (let n = kids.length - 1; n >= 0; n--) {
    kids[n].hidden = true;
    setText(more, "+" + (kids.length - n));
    if (el.scrollWidth <= el.clientWidth + 0.5) break;
  }
}
export function renderPills() {
  const el = $("#pills");
  const p = pillList();
  const key = JSON.stringify(p) + "|" + el.clientWidth;
  if (el._k === key) return;
  el._k = key;
  el.textContent = "";
  for (const [c, t] of p) { const s = h("span", "pill" + (c ? " " + c : "")); s.textContent = t; el.append(s); }
  if (p.length) el.append(h("span", "pill more"));
  fitPills(el);
}

// ------------------------------------------------------------------ status strip + alert line
export function muteWho() {
  const S = K.state, src = S ? String(S.mute_src || "") : "";
  const me = K.hello ? "phone " + K.hello.you : "";
  if (!src) return "";
  if (src === me) return "by this phone";
  if (src === "B4") return "by B4 on the box";
  if (src === "boot") return "since boot";
  return "by " + src;
}
export function renderStrip() {
  const S = K.state, ok = linked();
  const a = alerts(), serious = a.filter((x) => x[0] !== "info");
  const sl = $("#sLink");
  sl.className = ok ? "ok" : "bad";
  const L = K.link === "ok" && !K.fresh && K.lastBad ? K.lastBad : K.link;
  const slow = ok && linkSlow();
  if (slow) sl.className = "bad";
  setText(sl.lastChild, slow ? "SLOW" : ok ? "LINK" : L === "off" ? "OFF" : L === "connecting" ? "…" : "NO LINK");
  const run = ok && S && S.engine === "Running";
  $("#sRun").className = run ? "ok" : ok ? "bad" : "ph";
  setText($("#sRun").lastChild, run ? "RUN" : ok && S && S.engine ? String(S.engine).toUpperCase().slice(0, 8) : "RUN");
  const dev = S ? (S.device_present === false ? "NO DEV" : String(S.profile || S.device || "--").toUpperCase().slice(0, 6)) : "--";
  setText($("#sDev"), dev);
  const bd = $("#sBadge");
  bd.hidden = !a.length;
  setText(bd, "!" + a.length);
  bd.className = "badge" + (serious.some((x) => x[0] === "crit") ? " crit" : serious.length ? "" : " info");
  setText($("#sPh"), (S && num(S.phones) ? S.phones : 0) + " PH");
  const muted = ok && S && S.muted === true;
  const who = muteWho();
  setText($("#sMute"), "MUTED" + (who ? " · " + who : ""));
  $("#setupBtn").setAttribute("aria-expanded", String(UI.sheet === "setup"));
  const al = $("#alertLine"), top = serious[0];
  const wasHidden = al.hidden;
  al.hidden = !top;
  if (top) {
    al.className = "alert" + (top[0] === "crit" ? " crit" : "");
    al.dataset.own = top[2] ? "1" : "";
    setText(al.firstChild, top[1]);
    setText(al.lastChild, serious.length > 1 ? "+" + (serious.length - 1) : "");
  }
  const b = document.body;
  b.classList.toggle("muted", !!muted);
  b.classList.toggle("nolink", !ok);
  b.classList.toggle("held", !!(ok && S && S.muted && !holdUnmutes()));
  $("#frame").hidden = !muted;
  return wasHidden !== al.hidden;
}

// ------------------------------------------------------------------ meters (footer)
let mBars = null, mClips = null;
export function buildMeters() {
  const el = $("#meters");
  ["S", "S", "T", "T"].forEach((l, i) => {
    const m = h("div", "mtr", '<div class="clip"></div><div class="bar"><b></b></div><span></span>');
    m.lastChild.textContent = l;
    m.title = ["SUB L", "SUB R", "TOP L", "TOP R"][i];
    el.append(m);
  });
  mBars = $$(".mtr .bar b", el);
  mClips = $$(".mtr .clip", el);
}
export function drawMeters() {
  if (!mBars) return;
  const M = K.meters;
  if (!linked() || !M || !Array.isArray(M.l)) return;     // frozen, grey (CSS) on link loss
  for (let i = 0; i < 4; i++) {
    const v = num(M.l[i]) ? M.l[i] : -60;
    mBars[i].style.height = ((1 - clamp((v + 48) / 48, 0, 1)) * 100).toFixed(1) + "%";
    mClips[i].classList.toggle("on", !!(M.c && M.c[i]));
  }
}

// ------------------------------------------------------------------ layout of the overlays
export function layout() {
  const app = $("#app").getBoundingClientRect();
  const st = $("#strip").getBoundingClientRect();
  const ft = $("#foot").getBoundingClientRect();
  const top = st.bottom - app.top, bottom = app.bottom - ft.top;
  const sheet = $("#sheet");
  sheet.style.top = top + "px"; sheet.style.bottom = bottom + "px";
  $("#rotate").style.bottom = bottom + "px";
  const gr = $("#graph").getBoundingClientRect();
  $("#toast").style.bottom = (UI.sheet ? bottom + 12 : app.bottom - gr.bottom + 12) + "px";
}

export const countdown = (s, since) => mmss(Math.max(0, s - (now() - since) / 1000));
